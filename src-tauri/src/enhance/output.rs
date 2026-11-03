//! Strict parsing and validation of generated output. Anything other than one complete JSON
//! object `{"text": "..."}` that preserves every protected span is rejected; nothing is
//! "repaired" by stripping text.
use super::{
    prompt::{Prompt, MAX_OUTPUT_BYTES, MAX_OUTPUT_CHARS},
    EnhanceError, ErrorCode,
};
use regex::Regex;
use serde::Deserialize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Unchanged,
    Changed(String),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply {
    text: String,
}

fn invalid(message: &str) -> EnhanceError {
    EnhanceError::new(ErrorCode::InvalidOutput, message)
}

const NEGATIONS: &[&str] = &[
    "not",
    "no",
    "never",
    "cannot",
    "can't",
    "won't",
    "don't",
    "doesn't",
    "didn't",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "shouldn't",
    "wouldn't",
    "couldn't",
    "nothing",
    "nobody",
    "none",
    "neither",
    "nor",
    "without",
    "haven't",
    "hasn't",
    "hadn't",
    "mustn't",
    "needn't",
];
fn negations(text: &str) -> usize {
    text.split(|c: char| !(c.is_alphanumeric() || c == '\'' || c == '’'))
        .filter(|w| {
            let w = w.to_lowercase().replace('’', "'");
            NEGATIONS.contains(&w.as_str()) || w.ends_with("n't")
        })
        .count()
}
const PREAMBLES: &[&str] = &[
    "sure",
    "here is",
    "here's",
    "certainly",
    "of course",
    "okay, here",
    "ok, here",
    "rewritten",
    "revised text:",
    "the rewritten",
    "as an ai",
    "i can't help",
    "i cannot help",
];
fn preamble(text: &str) -> Option<&'static str> {
    let lower = text.trim_start().to_lowercase();
    PREAMBLES.iter().copied().find(|p| lower.starts_with(p))
}

fn markup(text: &str) -> bool {
    [
        "<think>",
        "</think>",
        "<|",
        "|>",
        "```",
        "`",
        "<tool_call>",
        "</tool_call>",
    ]
    .iter()
    .any(|tag| text.contains(tag))
}

fn introduced_values(original: &str, restored: &str) -> bool {
    // Compare multisets, not mere presence: adding a second technical value is also invention.
    let pattern = Regex::new(
        r#"(?i)(?:https?://|www\.)[^\s<>'"]+|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:[a-z]:\\|/|\./|\.\./)[\w./\\-]+|\b[\w.-]+/[\w./-]+|\b\d+(?:\.\d+)?\s?(?:(?:kg|km|mb|gb|ms|mph|°c|°f)\b|%)"#,
    ).expect("static technical-value pattern");
    let mut source: Vec<_> = pattern
        .find_iter(original)
        .map(|m| {
            m.as_str()
                .trim_end_matches(['.', ',', ';', ':', '!', '?'])
                .to_lowercase()
        })
        .collect();
    for value in pattern.find_iter(restored).map(|m| {
        m.as_str()
            .trim_end_matches(['.', ',', ';', ':', '!', '?'])
            .to_lowercase()
    }) {
        if let Some(pos) = source.iter().position(|item| item == &value) {
            source.remove(pos);
        } else {
            return true;
        }
    }
    false
}

fn interrogative(text: &str) -> bool {
    let first = text
        .trim_start_matches(|c: char| !c.is_alphabetic())
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_lowercase();
    matches!(
        first.as_str(),
        "how"
            | "what"
            | "why"
            | "when"
            | "where"
            | "who"
            | "whom"
            | "whose"
            | "which"
            | "can"
            | "could"
            | "would"
            | "should"
            | "will"
            | "do"
            | "does"
            | "did"
            | "is"
            | "are"
            | "was"
            | "were"
            | "may"
            | "might"
            | "have"
            | "has"
    )
}

pub fn within_limit(text: &str) -> bool {
    text.chars().count() <= MAX_OUTPUT_CHARS && text.len() <= MAX_OUTPUT_BYTES
}

pub fn parse(raw: &str, prompt: &Prompt, original: &str) -> Result<Outcome, EnhanceError> {
    let raw = raw.trim();
    for forbidden in ["<think>", "</think>", "<|", "|>", "```", "<tool_call>"] {
        if raw.contains(forbidden) {
            return Err(invalid("Model output contained markup"));
        }
    }
    if !raw.starts_with('{') || !raw.ends_with('}') {
        return Err(invalid("Model output was not a single JSON object"));
    }
    let reply: Reply =
        serde_json::from_str(raw).map_err(|_| invalid("Model output was not valid JSON"))?;
    let text = reply.text.trim();
    if text.is_empty() {
        return Err(invalid("Model returned empty text"));
    }
    if text.chars().count() > MAX_OUTPUT_CHARS || text.len() > MAX_OUTPUT_BYTES {
        return Err(invalid("Model output exceeded the size limit"));
    }
    if text.chars().count() > prompt.masked.chars().count() * 2 + 120 {
        return Err(invalid("Model output expanded too much"));
    }
    if markup(text) {
        return Err(invalid("Model output contained markup"));
    }
    // Every number was masked, so any digit in the output was invented or altered.
    if text.chars().any(|c| c.is_numeric()) {
        return Err(invalid("Model output changed a number"));
    }
    let p = &prompt.protected;
    let mut restored = text.to_owned();
    let mut expected = 0;
    for (marker, original_span) in &p.spans {
        let want = prompt.masked.matches(marker.as_str()).count();
        if text.matches(marker.as_str()).count() != want {
            return Err(invalid("Model output altered a protected value"));
        }
        expected += want;
        restored = restored.replace(marker.as_str(), original_span);
    }
    // No unknown or malformed markers.
    if text.matches(p.open).count() != expected || text.matches(p.close).count() != expected {
        return Err(invalid("Model output contained an unknown marker"));
    }
    if restored.chars().count() > MAX_OUTPUT_CHARS || restored.len() > MAX_OUTPUT_BYTES {
        return Err(invalid("Restored output exceeded the size limit"));
    }
    if introduced_values(original, &restored) {
        return Err(invalid("Model output introduced a technical value"));
    }
    if (original.trim_end().ends_with('?') || interrogative(original))
        && !restored.trim_end().ends_with('?')
        && restored.trim() != original.trim()
    {
        return Err(invalid("Model output answered instead of editing"));
    }
    if negations(&restored) < negations(original) {
        return Err(invalid("Model output dropped a negation"));
    }
    if let Some(p) = preamble(&restored) {
        if preamble(original) != Some(p) {
            return Err(invalid("Model output answered instead of editing"));
        }
    }
    if restored.trim() == original.trim() {
        Ok(Outcome::Unchanged)
    } else {
        Ok(Outcome::Changed(restored))
    }
}

#[cfg(test)]
mod tests {
    use super::super::prompt::{build, Preset};
    use super::*;
    fn run(input: &str, raw: &str) -> Result<Outcome, EnhanceError> {
        let prompt = build(input, Preset::Concise, &[]).unwrap();
        parse(raw, &prompt, input)
    }
    #[test]
    fn accepts_valid_rewrite_and_restores_markers() {
        let input = "um send the v2 build to bob@example.com";
        let prompt = build(input, Preset::Concise, &[]).unwrap();
        let raw = format!(
            "{{\"text\": {}}}",
            serde_json::to_string(&prompt.masked.replace("um send", "Send")).unwrap()
        );
        assert_eq!(
            parse(&raw, &prompt, input).unwrap(),
            Outcome::Changed("Send the v2 build to bob@example.com".into())
        );
    }
    #[test]
    fn unpunctuated_question_and_adjacent_technical_punctuation() {
        assert!(run(
            "how do I reset this",
            r#"{"text":"Open Settings and select Reset."}"#
        )
        .is_err());
        for (input, changed) in [
            ("visit https://example.com", "Visit https://example.com."),
            ("edit /etc/hosts", "Edit /etc/hosts."),
        ] {
            let prompt = build(input, Preset::Concise, &[]).unwrap();
            let raw = serde_json::json!({"text": prompt.masked.replace(
                if input.starts_with("visit") { "visit" } else { "edit" },
                if input.starts_with("visit") { "Visit" } else { "Edit" }) + "."})
            .to_string();
            assert_eq!(
                parse(&raw, &prompt, input).unwrap(),
                Outcome::Changed(changed.into())
            );
        }
    }
    #[test]
    fn unchanged_is_reported() {
        assert_eq!(
            run("Hello there.", r#"{"text":"Hello there."}"#).unwrap(),
            Outcome::Unchanged
        );
    }
    #[test]
    fn rejections() {
        let input = "please send it to bob at 5";
        for raw in [
            r#"Sure! {"text":"x"}"#,
            "```json\n{\"text\":\"x\"}\n```",
            r#"{"text":"x","extra":1}"#,
            r#"{"text":"x"}{"text":"y"}"#,
            r#"{"text":"x"#,
            r#"{"text":""}"#,
            r#"<think>hmm</think>{"text":"x"}"#,
            r#"{"text":"please send it to bob at 6"}"#,
            r#"{"text":"please send it to bob at"}"#,
            r#"{"text":"please send it to bob at [[PA]] [[PA]]"}"#,
            r#"{"text":"please send it to bob at [[PB]]"}"#,
            r#"{"text":"please send it to bob at [[PA]] [[x]]"}"#,
            r#"{"text":"<|im_start|>please send it to bob at [[PA]]"}"#,
        ] {
            assert!(run(input, raw).is_err(), "{raw}");
        }
        assert_eq!(
            run(input, r#"{"text":"Please send it to Bob at [[PA]]."}"#).unwrap(),
            Outcome::Changed("Please send it to Bob at 5.".into())
        );
    }
    #[test]
    fn questions_must_not_be_answered() {
        let q = "how do I reset this";
        assert!(run(q, r#"{"text":"Sure, here's how: open Settings."}"#).is_err());
        assert!(run(q, r#"{"text":"How do I reset this?"}"#).is_ok());
    }
    #[test]
    fn negation_loss_rejected() {
        assert!(run("do not ship it", r#"{"text":"Ship it."}"#).is_err());
        assert!(run("I don't think so", r#"{"text":"I do not think so."}"#).is_ok());
    }
    #[test]
    fn reviewer_counterexamples() {
        for (input, output) in [
            ("hello", "`hello`"),
            ("hello", "<tool_call>hello</tool_call>"),
            ("Send the update.", "Send the update to evil@example.com."),
            (
                "Send the update.",
                "Save notes/report.txt and send the update.",
            ),
            (
                "Send the update.",
                "Send the update to https://evil.example/x.",
            ),
            ("Send the update.", "Send the update in /etc/shadow."),
            ("How do I reset this?", "Open Settings and select Reset."),
            ("Please fix it.", "Revised text: Please fix it."),
        ] {
            let raw = serde_json::json!({"text":output}).to_string();
            assert!(run(input, &raw).is_err(), "accepted {output:?}");
        }
        let input = format!(
            "{}{}",
            "a ".repeat(46),
            "https://example.com/".to_owned() + &"x".repeat(4900)
        );
        let prompt = build(&input, Preset::Concise, &[]).unwrap();
        let raw = serde_json::json!({"text":prompt.masked}).to_string();
        assert!(
            parse(&raw, &prompt, &input).is_err(),
            "accepted oversized restoration"
        );
    }
    #[test]
    fn expansion_capped() {
        let long = format!("{{\"text\":\"{}\"}}", "word ".repeat(80));
        assert!(run("short", &long).is_err());
    }
}
