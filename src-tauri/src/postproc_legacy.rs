use crate::config::Config;
use regex::{Captures, RegexBuilder};
mod punctuation {
    include!("punctuation_legacy.rs");
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
pub fn process_style(text: &str, cfg: &Config, style: Style) -> String {
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
            let spoken_word = cfg.postproc.spoken_punctuation && punctuation::command_word(token);
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
        text = punctuation::spoken(&text, cfg.postproc.allow_newlines);
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
    for (i, original) in saved.iter().enumerate().rev() {
        text = text.replace(&format!("\u{e000}{i}\u{e001}"), original);
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
    let text = text.trim();
    if text.is_empty() {
        String::new()
    } else {
        format!(
            "{text}{}",
            if cfg.injection.trailing_space {
                " "
            } else {
                ""
            }
        )
    }
}
