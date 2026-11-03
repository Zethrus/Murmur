use regex::{Captures, Regex};

// A single alternation prevents cascades ("double equals" must not become "double =").
const MAP: &[(&str, &str)] = &[
    ("comma", ","),
    ("period", "."),
    ("full stop", "."),
    ("question mark", "?"),
    ("exclamation mark", "!"),
    ("exclamation point", "!"),
    ("colon", ":"),
    ("semicolon", ";"),
    ("open paren", "("),
    ("open parenthesis", "("),
    ("left paren", "("),
    ("close paren", ")"),
    ("right paren", ")"),
    ("open bracket", "["),
    ("close bracket", "]"),
    ("open brace", "{"),
    ("close brace", "}"),
    ("underscore", "_"),
    ("hyphen", "-"),
    ("dash", "-"),
    ("slash", "/"),
    ("backslash", "\\"),
    ("dot", "\u{e010}"),
    ("at sign", "@"),
    ("hash", "#"),
    ("asterisk", "*"),
    ("ampersand", "&"),
    ("pipe", "|"),
    ("equals", "="),
    ("double equals", "=="),
    ("arrow", "->"),
    ("fat arrow", "=>"),
    ("percent", "%"),
    ("dollar sign", "$"),
    ("plus", "+"),
    ("less than", "<"),
    ("greater than", ">"),
    ("backtick", "`"),
    ("newline", "\n"),
    ("next line", "\n"),
];
pub fn command_word(token: &str) -> bool {
    // Command lexemes are case-insensitive; arbitrary identifiers remain shielded.
    token.chars().all(|c| c.is_ascii_alphabetic())
        && (token.eq_ignore_ascii_case("quote")
            || token.eq_ignore_ascii_case("unquote")
            || MAP.iter().any(|(phrase, _)| {
                phrase
                    .split_whitespace()
                    .any(|word| word.eq_ignore_ascii_case(token))
            }))
}
fn replace(text: &str, pattern: &str, value: &str) -> String {
    Regex::new(pattern)
        .expect("static punctuation regex")
        .replace_all(text, value)
        .into_owned()
}
const SENTENCE: &str = ",.?!:;";
/// Collapse a spoken-command mark with an adjacent identical recogniser mark ("hello, comma"),
/// leaving intentional repeats ("...", "!!") that did not come from a command untouched.
fn collapse_spoken(text: &str) -> String {
    // (character, came-from-spoken-command)
    let mut items: Vec<(char, bool)> = Vec::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{e013}' {
            if let Some(mark) = chars.next() {
                items.push((mark, true));
            }
        } else {
            items.push((c, false));
        }
    }
    let solo = |items: &[(char, bool)], i: usize| {
        let mark = items[i].0;
        (i == 0 || items[i - 1] != (mark, false)) && items.get(i + 1) != Some(&(mark, false))
    };
    let mut drop = vec![false; items.len()];
    for i in 0..items.len() {
        if !items[i].1 {
            continue;
        }
        let mark = items[i].0;
        let prev = (0..i).rev().find(|&k| items[k].0 != ' ');
        let next = (i + 1..items.len()).find(|&k| items[k].0 != ' ');
        let pair = [prev, next]
            .into_iter()
            .flatten()
            .find(|&k| items[k].0 == mark && !items[k].1 && !drop[k] && solo(&items, k));
        if pair.is_some() {
            drop[i] = true;
        }
    }
    items
        .iter()
        .zip(drop)
        .filter(|(_, d)| !d)
        .map(|((c, _), _)| *c)
        .collect()
}
pub fn spoken(text: &str, newlines: bool) -> String {
    // Only balanced spoken quotes are changed. Unmatched quote/unquote stays literal.
    let text = Regex::new(r"(?i)\bquote\b(.*?)\bunquote\b")
        .expect("quote regex")
        .replace_all(text, |c: &Captures<'_>| {
            format!("\u{e011}{}\u{e012}", c[1].trim())
        })
        .into_owned();
    let mut phrases: Vec<_> = MAP.iter().map(|(s, _)| *s).collect();
    phrases.sort_by_key(|s| std::cmp::Reverse(s.len()));
    let pattern = format!(r"(?i)\b(?:{})\b", phrases.join("|"));
    let mut text = Regex::new(&pattern)
        .expect("literal phrase map")
        .replace_all(&text, |c: &Captures<'_>| {
            let symbol = MAP
                .iter()
                .find(|(p, _)| p.eq_ignore_ascii_case(&c[0]))
                .expect("matched map entry")
                .1;
            if symbol == "\n" && !newlines {
                " ".to_owned()
            } else if symbol.len() == 1 && SENTENCE.contains(symbol) {
                // Provenance marker: this mark came from a spoken command.
                format!("\u{e013}{symbol}")
            } else {
                symbol.to_owned()
            }
        })
        .into_owned();
    // Consecutive spoken marks join ("comma comma" -> ",,").
    loop {
        let joined = replace(&text, "(\u{e013}[,.?!:;]) +\u{e013}", "$1\u{e013}");
        if joined == text {
            break;
        }
        text = joined;
    }
    text = collapse_spoken(&text);
    text = replace(&text, r"[ \t]+", " ");
    text = replace(&text, r" *\n *", "\n");
    // Runs ("...", "!!", ").") stay together.
    text = replace(&text, r" *([,.:;!?\)\]\}]+) *", "$1 ");
    text = replace(&text, r#"([.!?,:;]) +""#, "$1\"");
    text = replace(&text, r"([\(\[\{]) +", "$1");
    // Dot is distinct from a spoken period: foo dot bar is an identifier, foo period bar is prose.
    text = replace(&text, r" *([_/\\@\x{e010}]) *", "$1");
    text = replace(&text, r"\x{e011} *", "\"");
    text = replace(&text, r" *\x{e012}", "\"");
    text.replace('\u{e010}', ".").trim().to_owned()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_phrase_is_mapped() {
        for (phrase, symbol) in MAP {
            let expected = if *symbol == "\n" {
                ""
            } else if *symbol == "\u{e010}" {
                "."
            } else {
                symbol
            };
            assert_eq!(spoken(phrase, true), expected, "{phrase}");
        }
    }
    #[test]
    fn longest_match_boundaries_and_case() {
        assert_eq!(
            spoken("a DOUBLE EQUALS b fat arrow c", false),
            "a == b => c"
        );
        assert_eq!(
            spoken("command hashbrown periodical slasher", false),
            "command hashbrown periodical slasher"
        );
    }
    #[test]
    fn human_spacing() {
        assert_eq!(
            spoken(
                "hello comma world full stop next colon item semicolon yes",
                false
            ),
            "hello, world. next: item; yes"
        );
        assert_eq!(
            spoken(
                "open paren x close paren open bracket y close bracket open brace z close brace",
                false
            ),
            "(x) [y] {z}"
        );
        assert_eq!(
            spoken(
                "foo underscore bar slash src backslash main dot rs at sign host",
                false
            ),
            "foo_bar/src\\main.rs@host"
        );
    }
    #[test]
    fn spoken_mark_collapses_with_recogniser_mark() {
        assert_eq!(spoken("hello, comma world", false), "hello, world");
        assert_eq!(spoken("done period. next", false), "done. next");
        assert_eq!(spoken("wait... really", false), "wait... really");
        assert_eq!(spoken("wow!! nice", false), "wow!! nice");
        assert_eq!(spoken("a comma comma b", false), "a,, b");
    }
    #[test]
    fn quotes_and_newlines() {
        assert_eq!(
            spoken("say quote hello comma world unquote now", false),
            "say \"hello, world\" now"
        );
        assert_eq!(spoken("quote missing", false), "quote missing");
        assert_eq!(
            spoken("one newline two next line three", false),
            "one two three"
        );
        assert_eq!(
            spoken("one newline two next line three", true),
            "one\ntwo\nthree"
        );
    }
}
