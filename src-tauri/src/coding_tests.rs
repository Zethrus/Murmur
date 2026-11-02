use crate::{
    config::Config,
    inject::{choose_path, safe_text, InjectionPath},
    postproc::{process_style, resolve_style, Style},
};
fn code(text: &str, c: &Config) -> String {
    process_style(text, c, Style::Code)
}
#[test]
fn dictionary_is_passed_to_the_decoder() {
    let c = Config::default();
    let params = crate::controller::params(&[0.0], &c);
    assert_eq!(params["initial_prompt"], c.engine.dictionary.join(", "));
}
#[test]
fn defaults_and_legacy_config() {
    let c: Config = toml::from_str("[general]\nhotkey='Ctrl+Alt+D'").unwrap();
    c.validate().unwrap();
    assert_eq!(c.audio.max_utterance_s, 90);
    assert_eq!(c.postproc.style, "auto");
    assert_eq!(c.postproc.code_apps.len(), 14);
    assert_eq!(c.engine.dictionary.len(), 30);
    assert!(c.engine.dictionary.contains(&"CTranslate2".into()));
    assert!(c.injection.prefer_paste && c.postproc.spoken_punctuation);
    assert!(!c.postproc.allow_newlines);
    assert_eq!(c.general.hotkey_code, "");
}
#[test]
fn auto_style_and_force() {
    let mut c = Config::default();
    for app in [
        "Code",
        "Cursor.exe",
        "jetbrains-idea",
        "Gnome-Terminal",
        "WindowsTerminal.exe",
    ] {
        assert_eq!(resolve_style(&c, app, false), Style::Code);
    }
    assert_eq!(resolve_style(&c, "firefox", false), Style::Prose);
    c.postproc.style = "prose".into();
    assert_eq!(resolve_style(&c, "code", false), Style::Prose);
    assert_eq!(resolve_style(&c, "firefox", true), Style::Code);
    c.postproc.style = "code".into();
    assert_eq!(resolve_style(&c, "firefox", false), Style::Code);
}
#[test]
fn code_preserves_identifiers_and_fillers() {
    let mut c = Config::default();
    c.postproc
        .replacements
        .insert("foo".into(), "changed".into());
    assert_eq!(code(".foo FOO foo.", &c), ".foo FOO foo. ");
    assert_eq!(
        code(
            "hello. next um foo_bar foo.rs foo/bar foo() foo{} foo::bar foo=>bar foo<bar> fooBar",
            &c
        ),
        "hello. next um foo_bar foo.rs foo/bar foo() foo{} foo::bar foo=>bar foo<bar> fooBar "
    );
    assert_eq!(code("foo underscore bar", &c), "foo_bar ");
    c.injection.remove_fillers = true;
    assert_eq!(code("um hello", &c), "hello ");
}
#[test]
fn punctuation_before_replacements_and_toggle() {
    let mut c = Config::default();
    c.postproc
        .replacements
        .insert("comma".into(), "wrong".into());
    assert_eq!(code("hello COMMA world", &c), "hello, world ");
    assert_eq!(code("hello CoMmA world", &c), "hello, world ");
    assert_eq!(
        code("quote hello comma world unquote", &c),
        "\"hello, world\" "
    );
    c.postproc.spoken_punctuation = false;
    assert_eq!(code("hello comma world", &c), "hello wrong world ");
}
#[test]
fn newlines_gated_even_in_replacements() {
    let mut c = Config::default();
    c.postproc
        .replacements
        .insert("break".into(), "one\ntwo".into());
    assert_eq!(
        code("hello newline world\nnext break", &c),
        "hello world next one two "
    );
    c.postproc.allow_newlines = true;
    assert_eq!(
        code("hello newline world\nnext break", &c),
        "hello\nworld\nnext one\ntwo "
    );
}
#[test]
fn paste_precedence_and_length_boundary() {
    let mut c = Config::default().injection;
    assert_eq!(
        choose_path("x", &c, "editor", Style::Code),
        InjectionPath::Clipboard
    );
    assert_eq!(
        choose_path(&"a".repeat(60), &c, "editor", Style::Prose),
        InjectionPath::Type
    );
    assert_eq!(
        choose_path(&"a".repeat(61), &c, "editor", Style::Prose),
        InjectionPath::Clipboard
    );
    c.method = "type".into();
    assert_eq!(
        choose_path("x", &c, "editor", Style::Code),
        InjectionPath::Clipboard
    );
    c.prefer_paste = false;
    assert_eq!(
        choose_path("x", &c, "editor", Style::Code),
        InjectionPath::Type
    );
    c.clipboard_only = true;
    assert_eq!(
        choose_path("x", &c, "editor", Style::Code),
        InjectionPath::ClipboardOnly
    );
    c.clipboard_only = false;
    c.exclude_apps.push("editor".into());
    assert_eq!(
        choose_path("x", &c, "Editor", Style::Code),
        InjectionPath::ClipboardOnly
    );
    c.clipboard_only = true;
    assert_eq!(
        choose_path("x", &c, "Murmur", Style::Code),
        InjectionPath::Skip
    );
}
#[test]
fn terminals_never_receive_enter() {
    for app in [
        "gnome-terminal",
        "Konsole",
        "kitty",
        "alacritty",
        "wezterm",
        "WindowsTerminal.exe",
        "mintty",
        "xterm",
        "powershell.exe",
    ] {
        assert_eq!(
            safe_text("one\r\ntwo\nthree\rfour", app),
            "one two three four"
        );
    }
    assert_eq!(safe_text("one\ntwo", "code"), "one\ntwo");
}
#[test]
fn second_hotkey_validation_and_map_deletion() {
    let c = Config::default();
    assert!(c
        .patched(serde_json::json!({"general":{"hotkey_code":"Control+Alt+D"}}))
        .is_err());
    assert!(c
        .patched(serde_json::json!({"general":{"hotkey_code":"Shift"}}))
        .is_err());
    assert!(c
        .patched(serde_json::json!({"general":{"hotkey_code":"Ctrl+Shift+D"}}))
        .is_ok());
    assert!(c
        .patched(serde_json::json!({"postproc":{"replacements":{}}}))
        .unwrap()
        .postproc
        .replacements
        .is_empty());
}
