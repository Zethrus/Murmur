use crate::config::Config;
use regex::{Captures, RegexBuilder};

// Frozen v0.1.2 dictation processor. Enhance OFF must not change delivery bytes.
mod legacy {
    include!("postproc_legacy.rs");
}

fn protected(token: &str) -> bool {
    let clean =
        token.trim_matches(|c: char| matches!(c, '.' | ',' | '!' | '?' | ';' | ':' | '"' | '\''));
    clean.chars().any(|c| c.is_numeric())
        || clean.contains("://")
        || clean.contains('@')
        || clean.chars().any(|c| "_`/\\={}[]<>():".contains(c))
        || clean.chars().skip(1).any(char::is_uppercase)
        || clean.contains('.')
}
fn protected_style(token: &str, style: Style) -> bool {
    protected(token) || (style == Style::Code && token.chars().any(|c| "_./(){}<>".contains(c)))
}
fn substitute(text: &str, phrase: &str, replacement: &str) -> String {
    let punctuation = if replacement.is_empty() { "[,;]?" } else { "" };
    let pattern = format!(r"\b{}\b{}", regex::escape(phrase), punctuation);
    match RegexBuilder::new(&pattern).case_insensitive(true).build() {
        Ok(re) => re
            .replace_all(text, |cap: &Captures<'_>| replacement_for(cap, replacement))
            .into_owned(),
        Err(_) => text.to_owned(),
    }
}
fn replacement_for(cap: &Captures<'_>, replacement: &str) -> String {
    if cap[0].split_whitespace().any(protected) {
        cap[0].to_owned()
    } else {
        replacement.to_owned()
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Style {
    Prose,
    Code,
}
pub fn resolve_style(cfg: &Config, focused: &str, force: bool) -> Style {
    let app = focused.to_lowercase();
    if force
        || cfg.postproc.style == "code"
        || (cfg.postproc.style == "auto"
            && cfg
                .postproc
                .code_apps
                .iter()
                .any(|s| !s.trim().is_empty() && app.contains(&s.to_lowercase())))
    {
        Style::Code
    } else {
        Style::Prose
    }
}
#[cfg(test)]
pub fn process(text: &str, cfg: &Config) -> String {
    process_style(text, cfg, resolve_style(cfg, "", false))
}
fn normalize(text: &str, newlines: bool) -> String {
    if newlines {
        text.replace("\r\n", "\n")
            .replace('\r', "\n")
            .split('\n')
            .map(|s| s.split_whitespace().collect::<Vec<_>>().join(" "))
            .collect::<Vec<_>>()
            .join("\n")
    } else {
        text.split_whitespace().collect::<Vec<_>>().join(" ")
    }
}
/// Shield input characters used by internal markers with fresh, collision-checked codepoints.
/// The reverse pass only maps codepoints selected for this invocation, never user text.
fn shield_markers(text: &str) -> (String, Vec<(char, char)>) {
    let mut mappings = Vec::new();
    let mut result = text.to_owned();
    for original in text
        .chars()
        .filter(|c| (0xE000..=0xE0FF).contains(&(*c as u32)))
    {
        if mappings.iter().any(|(known, _)| *known == original) {
            continue;
        }
        let replacement = (0x100000..=0x10FFFD)
            .filter_map(char::from_u32)
            .find(|candidate| {
                !text.contains(*candidate) && !mappings.iter().any(|(_, used)| used == candidate)
            })
            .expect("enough unused private codepoints");
        result = result.replace(original, &replacement.to_string());
        mappings.push((original, replacement));
    }
    (result, mappings)
}
const DUPLICATE_EXCEPTIONS: &[&str] = &["had", "that", "can", "do", "very"];
/// Deduplication alone shields quotes; punctuation, fillers and configured replacements
/// continue to use the legacy dictation pipeline. Newline markers survive until formatting.
fn remove_duplicates(text: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    let mut quoted: Option<char> = None;
    let mut code = false;
    let mut previous_word: Option<&str> = None;
    for token in text.split(' ') {
        let outside = quoted.is_none() && !code;
        let word = outside && !token.is_empty() && token.chars().all(char::is_alphabetic);
        let duplicate = word
            && previous_word.is_some_and(|prev| {
                prev.to_lowercase() == token.to_lowercase()
                    && !DUPLICATE_EXCEPTIONS.contains(&token.to_lowercase().as_str())
            });
        if !duplicate {
            out.push(token);
        }
        // Inspect the entire token, including punctuation-wrapped opening quotes. An
        // apostrophe between letters is a contraction, not a quote delimiter.
        let chars: Vec<char> = token.chars().collect();
        for (i, &ch) in chars.iter().enumerate() {
            if ch == '`' {
                code = !code;
            } else if !code {
                let contraction = ch == '\''
                    && i > 0
                    && i + 1 < chars.len()
                    && chars[i - 1].is_alphabetic()
                    && chars[i + 1].is_alphabetic();
                if contraction {
                    continue;
                }
                if quoted == Some(ch) {
                    quoted = None;
                } else if quoted.is_none() {
                    let closing = match ch {
                        '“' => Some('”'),
                        '‘' => Some('’'),
                        '"' => Some('"'),
                        '\'' if !(i > 0
                            && i + 1 < chars.len()
                            && chars[i - 1].is_alphabetic()
                            && chars[i + 1].is_alphabetic()) =>
                        {
                            Some('\'')
                        }
                        _ => None,
                    };
                    quoted = closing;
                }
            }
        }
        previous_word = if quoted.is_none() && !code && word && !duplicate {
            Some(token)
        } else {
            None
        };
    }
    out.join(" ")
}
/// Attach standalone sentence punctuation to the previous token ("hello , world").
fn tidy_punctuation(text: &str) -> String {
    let mut out: Vec<String> = Vec::new();
    for token in text.split(' ').filter(|t| !t.is_empty()) {
        let standalone = token.chars().all(|c| ",.;:!?".contains(c));
        match out.last_mut() {
            Some(prev)
                if standalone
                    && !prev.ends_with('\u{e020}')
                    && prev.chars().last().is_some_and(|c| !c.is_whitespace()) =>
            {
                prev.push_str(token)
            }
            _ => out.push(token.to_owned()),
        }
    }
    out.join(" ")
}
/// Deterministic, style-aware cleanup of the transcript body. Always runs (with or without
/// Enhance); each rule still honours its own configuration switch. No trailing separator.
pub fn clean_body(text: &str, cfg: &Config, style: Style) -> String {
    if !cfg.enhance.auto_rewrite {
        let old_style = match style {
            Style::Prose => legacy::Style::Prose,
            Style::Code => legacy::Style::Code,
        };
        let delivered = legacy::process_style(text, cfg, old_style);
        return if cfg.injection.trailing_space {
            delivered.strip_suffix(' ').unwrap_or(&delivered).to_owned()
        } else {
            delivered
        };
    }
    let (shielded, mappings) = shield_markers(text);
    let mut result = clean_inner(&shielded, cfg, style);
    for (original, replacement) in mappings {
        result = result.replace(replacement, &original.to_string());
    }
    result
}
/// Delivery formatting only: terminal safety, newline policy and exactly one trailing
/// separator. Used for both dictation and accepted rewrites (which skip `clean_body`).
pub fn format_delivery(body: &str, cfg: &Config, focused: &str) -> (String, String) {
    let body = if cfg.postproc.allow_newlines {
        body.trim().to_owned()
    } else {
        normalize(body, false)
    };
    let body = crate::inject::safe_text(&body, focused);
    let body = body.trim();
    if body.is_empty() {
        return (String::new(), String::new());
    }
    let separator = if cfg.injection.trailing_space {
        " "
    } else {
        ""
    };
    (format!("{body}{separator}"), separator.to_owned())
}
#[cfg(test)]
pub fn process_style(text: &str, cfg: &Config, style: Style) -> String {
    process_cleaned(&clean_body(text, cfg, style), cfg)
}

pub fn process_cleaned(body: &str, cfg: &Config) -> String {
    if body.is_empty() {
        String::new()
    } else {
        format!(
            "{body}{}",
            if cfg.injection.trailing_space {
                " "
            } else {
                ""
            }
        )
    }
}
fn clean_inner(text: &str, cfg: &Config, style: Style) -> String {
    // Keep physical line boundaries until deduplication, even when delivery flattens them.
    let normalized = normalize(
        text,
        cfg.postproc.allow_newlines || cfg.enhance.auto_rewrite,
    );
    let normalized = normalized.replace('\n', " \u{e020} ");
    // Protect complete tokens, not just regex matches inside URLs or code.
    let mut saved = Vec::new();
    let mut in_code = false;
    let mut text = normalized
        .split(' ')
        .map(|token| {
            let code = in_code || token.contains('`');
            if token.matches('`').count() % 2 == 1 {
                in_code = !in_code;
            }
            let spoken_word =
                cfg.postproc.spoken_punctuation && crate::punctuation::command_word(token);
            if code || (protected_style(token, style) && !spoken_word) {
                let marker = format!("\u{e000}{}\u{e001}", saved.len());
                saved.push(token.to_owned());
                marker
            } else {
                token.to_owned()
            }
        })
        .collect::<Vec<_>>()
        .join(" ");
    if cfg.postproc.spoken_punctuation {
        text = crate::punctuation::spoken(&text, cfg.postproc.allow_newlines);
    }
    text = text.replace('\n', " \u{e020} ");
    text = text
        .split(' ')
        .map(|token| {
            if !token.contains('\u{e000}') && protected_style(token, style) {
                let marker = format!("\u{e000}{}\u{e001}", saved.len());
                saved.push(token.to_owned());
                marker
            } else {
                token.to_owned()
            }
        })
        .collect::<Vec<_>>()
        .join(" ");
    if cfg.injection.remove_fillers {
        for filler in &cfg.postproc.fillers {
            text = substitute(&text, filler, "");
        }
    }
    let mut replacements: Vec<_> = cfg.postproc.replacements.iter().collect();
    replacements.sort_by_key(|(key, _)| std::cmp::Reverse(key.len()));
    // Shield replacements from subsequent shorter matches; replacements are not recursive.
    for (phrase, value) in replacements {
        let marker = format!("\u{e000}{}\u{e001}", saved.len());
        let next = substitute(&text, phrase, &marker);
        saved.push(value.clone());
        text = next;
    }
    text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    // After replacements (whose results are shielded markers), so "mur mur" -> "Murmur" wins.
    if style == Style::Prose {
        text = tidy_punctuation(&text);
    }
    // Single pass: restored originals are never rescanned for markers.
    let marker = regex::Regex::new("\u{e000}([0-9]+)\u{e001}").expect("static marker regex");
    text = marker
        .replace_all(&text, |cap: &Captures<'_>| {
            cap[1]
                .parse::<usize>()
                .ok()
                .and_then(|i| saved.get(i))
                .cloned()
                .unwrap_or_default()
        })
        .into_owned();
    if cfg.enhance.auto_rewrite && cfg.postproc.remove_duplicate_words && style == Style::Prose {
        text = remove_duplicates(&text);
    }
    if cfg.injection.capitalize && style == Style::Prose {
        let mut in_code = false;
        let mut sentence = true;
        text = text
            .split(' ')
            .map(|token| {
                let code = in_code || token.contains('`');
                if token.matches('`').count() % 2 == 1 {
                    in_code = !in_code;
                }
                let result = if sentence && !code && !protected(token) {
                    match token.char_indices().find(|(_, c)| c.is_alphabetic()) {
                        Some((i, c)) => format!(
                            "{}{}{}",
                            &token[..i],
                            c.to_uppercase(),
                            &token[i + c.len_utf8()..]
                        ),
                        None => token.to_owned(),
                    }
                } else {
                    token.to_owned()
                };
                if token.chars().any(char::is_alphanumeric) || token.starts_with('\u{e000}') {
                    sentence = false;
                }
                if token
                    .trim_end_matches(['"', '\'', ')', ']', '}'])
                    .ends_with(['.', '!', '?'])
                {
                    sentence = true;
                }
                result
            })
            .collect::<Vec<_>>()
            .join(" ");
    }
    let text = text.replace(" \u{e020} ", "\n").replace('\u{e020}', "\n");
    let text = normalize(&text, cfg.postproc.allow_newlines);
    text.trim().to_owned()
}
#[cfg(test)]
mod tests {
    use super::*;
    mod release {
        include!("postproc_baseline.rs");
    }
    #[test]
    fn enhance_off_matches_release_corpus() {
        let samples = [
            "hello there. next one!",
            "the the release",
            "the\nthe release",
            "I don't think that that is right",
            "say (\"please go go now\")",
            "'don't go go now' please the the end",
            "\"v2 please go go now\"",
            "say quote hello comma world unquote",
            "hello comma world",
            "`let mur mur = 2`",
            "foo_bar camelCase https://example.com /etc/hosts",
            "first line\nsecond line",
            "one newline two",
            "wait... really!!",
        ];
        let mut configs = Vec::new();
        configs.push(Config::default());
        let mut configured = Config::default();
        configured.injection.remove_fillers = true;
        configured
            .postproc
            .replacements
            .insert("please".into(), "kindly".into());
        configs.push(configured);
        for cfg in &configs {
            assert!(!cfg.enhance.auto_rewrite);
            for sample in samples {
                for style in [Style::Prose, Style::Code] {
                    let old_style = match style {
                        Style::Prose => release::Style::Prose,
                        Style::Code => release::Style::Code,
                    };
                    assert_eq!(
                        process_style(sample, cfg, style),
                        release::process_style(sample, cfg, old_style),
                        "{sample:?} {style:?}"
                    );
                }
            }
        }
    }
    #[test]
    fn dedup_boundaries_when_enabled() {
        let mut c = Config::default();
        c.enhance.auto_rewrite = true;
        assert_eq!(process("the\nthe release", &c), "The the release ");
        assert_eq!(
            process("say (\"please go go now\")", &c),
            "Say (\"please go go now\") "
        );
        assert_eq!(
            process("'don't go go now' please the the end", &c),
            "'Don't go go now' please the end "
        );
        assert_eq!(process("the the release", &c), "The release ");
        c.postproc.replacements.insert("go".into(), "leave".into());
        assert_eq!(process("say 'go go'", &c), "Say 'leave leave' ");
        assert_eq!(
            process("say 'hello comma world'", &c),
            "Say 'hello, world' "
        );
    }
    #[test]
    fn whitespace_and_sentences() {
        assert_eq!(
            process("  hello   there. next one!", &Config::default()),
            "Hello there. Next one! "
        );
    }
    #[test]
    fn whole_word_fillers() {
        let mut c = Config::default();
        c.injection.remove_fillers = true;
        assert_eq!(process("um umbrella uh hello", &c), "Umbrella hello ");
        assert_eq!(process("um uh", &c), "");
    }
    #[test]
    fn longest_case_insensitive() {
        let mut c = Config::default();
        c.postproc.replacements.insert("mur".into(), "bad".into());
        assert_eq!(process("Mur mur", &c), "Murmur ");
    }
    #[test]
    fn punctuation_and_code_spans() {
        let mut config = Config::default();
        config.injection.remove_fillers = true;
        assert_eq!(
            process("um, hello iPhone. next", &config),
            "Hello iPhone. Next "
        );
        assert_eq!(process("`let mur mur = 2`", &config), "`let mur mur = 2` ");
        assert_eq!(process("\"hello.\" next", &config), "\"Hello.\" Next ");
    }
    #[test]
    #[ignore = "opt-in performance measurement"]
    fn pre_injection_cleanup_budget() {
        let config = Config::default();
        let input = "um please send the release notes to the team when the build finishes";
        let start = std::time::Instant::now();
        for _ in 0..1_000 {
            let cleaned = clean_body(input, &config, Style::Prose);
            std::hint::black_box(process_cleaned(&cleaned, &config));
        }
        eprintln!(
            "clean + delivery formatting, 1000 runs: {:?}",
            start.elapsed()
        );
    }
    #[test]
    fn reviewer_boundaries_and_unicode() {
        let mut c = Config::default();
        c.postproc.allow_newlines = true;
        assert_eq!(process("the\nthe release", &c), "The\nthe release ");
        assert_eq!(
            process("say 'please go go now'", &c),
            "Say 'please go go now' "
        );
        assert_eq!(
            process("\"v2 please go go now\"", &c),
            "\"v2 please go go now\" "
        );
        for original in ["\u{100020}", "\u{100013}"] {
            assert_eq!(process(original, &c).trim(), original);
        }
    }
    #[test]
    fn protected_tokens() {
        let c = Config::default();
        assert_eq!(
            process("iPhone 12.5 https://mur.example/a foo_bar camelCase", &c),
            "iPhone 12.5 https://mur.example/a foo_bar camelCase "
        );
    }
}
