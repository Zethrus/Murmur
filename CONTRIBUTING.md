# Contributing to Murmur

For a bug report, include your Murmur version, Linux distribution, desktop,
X11/Wayland session type, and the steps needed to reproduce the problem. For
companion issues, include the Flipper firmware and connection type. Remove
transcripts, pairing tokens, and personal paths from logs before sharing them.

Open an issue before a large feature or architectural change. Small fixes can go
straight to a pull request. Keep each patch focused, describe what changes for the
user, and include a regression test when fixing behavior.

Start with [the build guide](docs/building.md). Run the checks relevant to your
change and include their results in the pull request. Changes to the companion
protocol must update the specification and both implementations together. Version
checks run during the frontend build.

Fork pull requests do not run on the project's self-hosted runner. A maintainer
must review them before running their code on that machine. Windows and Android
are deferred; avoid presenting untested support as available.

Contributions are made under the project's [GPL version 3 license](LICENSE).
Do not include code, assets, or model files that you cannot redistribute under
compatible terms.
