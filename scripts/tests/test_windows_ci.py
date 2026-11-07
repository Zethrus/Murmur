"""Static guards for the Windows CI workflow and the Edison provisioning script.

Text-based on purpose (no PyYAML on the CI interpreters): each workflow step is the block of
lines from its "      - " item to the next one at the same indentation.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def steps(workflow):
    """Every step block of a workflow (6-space list items under `steps:`)."""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    blocks, current = [], None
    for line in text.splitlines():
        if line.startswith("      - "):
            current = [line]
            blocks.append(current)
        elif current is not None and (line.startswith("        ") or not line.strip()):
            current.append(line)
        else:
            current = None
    return ["\n".join(block) for block in blocks]


def step(workflow, name_prefix):
    found = [block for block in steps(workflow) if re.search(r"- name: " + re.escape(name_prefix), block)]
    if len(found) != 1:
        raise AssertionError(f"expected one step named {name_prefix!r} in {workflow}, found {len(found)}")
    return found[0]


def condition(block):
    match = re.search(r"^        if: (.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


class WindowsBuildSigningTests(unittest.TestCase):
    def test_certificate_import_runs_only_for_release_tags(self):
        block = step("windows-build.yml", "Import the Authenticode certificate")
        self.assertEqual(condition(block), "startsWith(github.ref, 'refs/tags/')")
        self.assertIn("secrets.WINDOWS_CERT_PFX_BASE64", block)

    def test_only_the_tag_only_import_step_sees_certificate_secrets(self):
        with_secrets = [block for block in steps("windows-build.yml") if "secrets." in block]
        self.assertEqual(len(with_secrets), 1)
        self.assertIn("Import the Authenticode certificate", with_secrets[0])

    def test_build_signs_only_on_tags_and_removal_always_runs(self):
        build = step("windows-build.yml", "Build murmur.exe")
        self.assertIn("$env:MURMUR_SIGN_THUMBPRINT -and $env:GITHUB_REF -like 'refs/tags/*'", build)
        self.assertEqual(condition(step("windows-build.yml", "Remove the Authenticode certificate")), "always()")

    def test_release_inherits_secrets_only_from_tag_pushes(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        trigger = text.split("\npermissions:")[0]
        self.assertRegex(trigger, r"on:\n  push:\n    tags: \['v\*'\]\n*$")
        self.assertEqual(text.count("secrets: inherit"), 1)
        windows_job = text.split("\n  windows:\n", 1)[1].split("\n  linux:\n", 1)[0]
        self.assertIn("secrets: inherit", windows_job)

    def test_powershell_never_assigns_the_automatic_args_variable(self):
        for workflow in WORKFLOWS.glob("*.yml"):
            with self.subTest(workflow=workflow.name):
                self.assertNotRegex(workflow.read_text(encoding="utf-8"), r"\$Args\b")


class WindowsSmokeTests(unittest.TestCase):
    def test_cli_smoke_runs_everywhere_and_launch_only_on_hosted_runners(self):
        cli = step("windows-build.yml", "Startup smoke test (--version, --help)")
        self.assertIsNone(condition(cli))
        self.assertIn("--version", cli)
        launch = step("windows-build.yml", "Startup smoke test (launch")
        self.assertEqual(condition(launch), "vars.WINDOWS_RUNNER == ''")
        self.assertIn("'--background'", launch)


class ProvisioningSecretTests(unittest.TestCase):
    SECRETS = ("$Password", "$RunnerToken")

    def setUp(self):
        self.script = (ROOT / "scripts" / "windows" / "provision-runner.ps1").read_text(encoding="utf-8")

    def logical_lines(self):
        """Lines with PowerShell backtick continuations joined."""
        return re.sub(r"`\r?\n\s*", " ", self.script).splitlines()

    def invoke_native_calls(self):
        """Each Invoke-Native statement, including argument arrays continued over several lines."""
        lines = self.logical_lines()
        for index, line in enumerate(lines):
            if "Invoke-Native " not in line or line.lstrip().startswith("function"):
                continue
            statement = line
            while statement.count("(") > statement.count(")") and index + 1 < len(lines):
                index += 1
                statement += " " + lines[index]
            yield statement

    def test_invoke_native_never_receives_a_secret(self):
        # Its error message repeats every argument into the Start-Transcript log.
        calls = list(self.invoke_native_calls())
        self.assertGreater(len(calls), 5)
        for statement in calls:
            for secret in self.SECRETS:
                self.assertNotIn(secret, statement)

    def test_messages_never_interpolate_a_secret(self):
        for line in self.logical_lines():
            for command in ("Write-Host", "Write-Output", "Write-Warning", "Write-Error", "throw "):
                if command in line:
                    for literal in re.findall(r'"[^"]*"', line):
                        for secret in self.SECRETS:
                            self.assertNotIn(secret, literal, line)

    def test_secret_commands_fail_with_fixed_messages(self):
        lines = self.logical_lines()
        config = next(index for index, line in enumerate(lines) if "config.cmd --url" in line)
        self.assertIn("--token $RunnerToken", lines[config])
        self.assertRegex(lines[config + 1], r'throw "Runner registration \(config\.cmd\) failed with exit code \$LASTEXITCODE')
        cmdkey = next(index for index, line in enumerate(lines) if line.lstrip().startswith("& cmdkey"))
        self.assertIn("*> $null", lines[cmdkey])
        self.assertNotIn("$Password", lines[cmdkey + 1])

    def test_driver_floor_is_the_cuda_12_windows_minimum(self):
        self.assertIn("[string]$MinimumDriver = '528.33'", self.script)
        self.assertIn("[version]$Driver -lt [version]$MinimumDriver", self.script)


if __name__ == "__main__":
    unittest.main()
