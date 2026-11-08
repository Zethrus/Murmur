//! Pure overlay placement tests: negative origins, scaling, monitor removal, oversize views.
use crate::{
    config::{Overlay, Placement},
    overlay::{choose_display, clamp_size, display_for, place, placement_for, Display, Rect},
};

fn display(name: &str, x: i32, y: i32, w: u32, h: u32, scale: f64) -> Display {
    Display {
        name: name.into(),
        bounds: Rect { x, y, w, h },
        work: Rect {
            x,
            y: y + 30,
            w,
            h: h - 30,
        },
        scale,
    }
}

#[test]
fn preset_positions_center_on_primary() {
    let d = display("A", 0, 0, 1920, 1080, 1.0);
    let mut prefs = Overlay::default();
    let (x, y) = place(&prefs, &d, 600, 100);
    assert_eq!(x, 660);
    assert_eq!(y, 30 + 1050 - 100 - 28);
    prefs.position = "top-center".into();
    assert_eq!(place(&prefs, &d, 600, 100).1, 30 + 28);
}

#[test]
fn custom_placement_roundtrips_on_negative_origin() {
    let left = display("LEFT", -2560, -200, 2560, 1440, 2.0);
    let window = Rect {
        x: -2000,
        y: 500,
        w: 800,
        h: 200,
    };
    let p = placement_for(&left, &window);
    assert_eq!(
        (p.monitor_x, p.monitor_y, p.monitor_width),
        (-2560, -200, 2560)
    );
    let prefs = Overlay {
        position: "custom".into(),
        placement: p,
        ..Overlay::default()
    };
    assert_eq!(place(&prefs, &left, 800, 200), (window.x, window.y));
}

#[test]
fn monitor_selection_and_removal_fallback() {
    let displays = vec![
        display("PRIMARY", 0, 0, 1920, 1080, 1.0),
        display("RIGHT", 1920, 0, 2560, 1440, 1.5),
    ];
    let saved = placement_for(
        &displays[1],
        &Rect {
            x: 2000,
            y: 100,
            w: 400,
            h: 100,
        },
    );
    assert_eq!(choose_display(&displays, &saved), 1);
    // Same name, moved geometry.
    let moved = vec![
        displays[0].clone(),
        display("RIGHT", -2560, 0, 2560, 1440, 1.5),
    ];
    assert_eq!(choose_display(&moved, &saved), 1);
    // Monitor disconnected: primary.
    assert_eq!(choose_display(&displays[..1], &saved), 0);
    assert_eq!(choose_display(&displays, &Placement::default()), 0);
    assert_eq!(
        display_for(
            &displays,
            &Rect {
                x: 1900,
                y: 0,
                w: 400,
                h: 100
            }
        ),
        Some(1)
    );
    assert_eq!(
        display_for(
            &displays,
            &Rect {
                x: 9000,
                y: 0,
                w: 10,
                h: 10
            }
        ),
        None
    );
}

#[test]
fn oversized_views_shrink_and_stay_inside() {
    let d = display("SMALL", 0, 0, 800, 600, 1.0);
    let (w, h) = clamp_size(&d.work, 1200, 900);
    assert_eq!((w, h), (800, 570));
    let prefs = Overlay {
        position: "custom".into(),
        placement: Placement {
            x_fraction: 1.0,
            y_fraction: 1.0,
            ..placement_for(
                &d,
                &Rect {
                    x: 0,
                    y: 0,
                    w: 10,
                    h: 10,
                },
            )
        },
        ..Overlay::default()
    };
    let (x, y) = place(&prefs, &d, w, h);
    assert_eq!((x, y), (0, 30));
    let (x, y) = place(&prefs, &d, 200, 100);
    assert!(x + 200 <= 800 && y + 100 <= 600);
}

#[test]
fn size_reports_are_validated_and_revisioned() {
    use crate::overlay::accept_size;
    assert!(accept_size(f64::NAN, 10.0, 1_000, "first").is_err());
    assert!(accept_size(10.0, -1.0, 1_001, "first").is_err());
    let first = accept_size(5000.0, 10.0, 1_002, "first").unwrap().unwrap();
    assert_eq!(first, (1200.0, 40.0));
    assert!(
        accept_size(300.0, 300.0, 1_002, "first").unwrap().is_none(),
        "stale revision ignored"
    );
    assert_eq!(
        accept_size(300.0, 300.0, 1, "reloaded").unwrap(),
        Some((300.0, 300.0))
    );
    // The idle icon reports about 44x44; the window keeps no 120 px pill floor.
    assert_eq!(
        accept_size(44.0, 44.0, 2, "reloaded").unwrap(),
        Some((44.0, 44.0))
    );
    assert_eq!(
        accept_size(1.0, 1.0, 3, "reloaded").unwrap(),
        Some(crate::overlay::MIN_SIZE)
    );
}

#[test]
fn presets_ignore_a_previous_custom_monitor() {
    let displays = vec![
        display("PRIMARY", 0, 0, 1920, 1080, 1.0),
        display("LEFT", -2560, 0, 2560, 1440, 1.5),
    ];
    let mut prefs = Overlay {
        position: "custom".into(),
        placement: placement_for(
            &displays[1],
            &Rect {
                x: -2000,
                y: 200,
                w: 300,
                h: 100,
            },
        ),
        ..Overlay::default()
    };
    assert_eq!(crate::overlay::display_index(&displays, &prefs), 1);
    for preset in ["top-center", "bottom-center"] {
        prefs.position = preset.into();
        assert_eq!(crate::overlay::display_index(&displays, &prefs), 0);
    }
}
