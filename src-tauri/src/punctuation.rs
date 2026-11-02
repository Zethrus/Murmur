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
            } else {
                symbol.to_owned()
            }
        })
        .into_owned();
    text = replace(&text, r"[ \t]+", " ");
    text = replace(&text, r" *\n *", "\n");
    text = replace(&text, r" *([,.:;!?\)\]\}]) *", "$1 ");
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
