use crate::config::Config;
use regex::{Captures, RegexBuilder};

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
/// Internal markers live in U+E000..=U+E0FF. Swapping that block with U+100000..=U+1000FF is a
/// bijection, so input that already contains marker characters cannot collide with them.
fn swap_private(text: &str) -> String {
    text.chars()
        .map(|c| match c as u32 {
            n @ 0xE000..=0xE0FF => char::from_u32(n - 0xE000 + 0x10_0000).unwrap_or(c),
            n @ 0x10_0000..=0x10_00FF => char::from_u32(n - 0x10_0000 + 0xE000).unwrap_or(c),
            _ => c,
        })
        .collect()
}
const DUPLICATE_EXCEPTIONS: &[&str] = &["had", "that", "can", "do", "very"];
/// Collapse adjacent, case-insensitive, purely alphabetic duplicate words. Tokens with
/// punctuation, markers (protected spans, newlines) or quotes never participate.
fn remove_duplicates(text: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    let mut in_quote = false;
    for token in text.split(' ') {
        let word = !token.is_empty() && token.chars().all(char::is_alphabetic);
        let duplicate = word
            && !in_quote
            && out.last().is_some_and(|prev| {
                prev.chars().all(char::is_alphabetic)
                    && prev.to_lowercase() == token.to_lowercase()
                    && !DUPLICATE_EXCEPTIONS.contains(&token.to_lowercase().as_str())
            });
        if token.matches('"').count() % 2 == 1 {
            in_quote = !in_quote;
        }
        if !duplicate {
            out.push(token);
        }
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
    swap_private(&clean_inner(&swap_private(text), cfg, style))
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
pub fn process_style(text: &str, cfg: &Config, style: Style) -> String {
    let body = clean_body(text, cfg, style);
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
    let normalized = normalize(text, cfg.postproc.allow_newlines);
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
        if cfg.postproc.remove_duplicate_words {
            text = remove_duplicates(&text.split_whitespace().collect::<Vec<_>>().join(" "));
        }
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
    fn protected_tokens() {
        let c = Config::default();
        assert_eq!(
            process("iPhone 12.5 https://mur.example/a foo_bar camelCase", &c),
            "iPhone 12.5 https://mur.example/a foo_bar camelCase "
        );
    }
}
