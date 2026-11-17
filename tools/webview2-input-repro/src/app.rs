//! Windows host. See the crate documentation in main.rs.

pub use crate::logic::json_string;
use crate::logic::{hand_back, page_message, parse_args, HostInput};
use std::{
    cell::RefCell,
    fs::File,
    io::Write,
    sync::{mpsc, Mutex, OnceLock},
    time::Instant,
};
use tao::{
    dpi::{LogicalSize, PhysicalPosition},
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy},
    platform::windows::{WindowBuilderExtWindows, WindowExtWindows},
    window::{Window, WindowBuilder},
};
use webview2_com::{
    take_pwstr, CoreWebView2EnvironmentOptions, CreateCoreWebView2ControllerCompletedHandler,
    CreateCoreWebView2EnvironmentCompletedHandler, Microsoft::Web::WebView2::Win32::*,
    WebMessageReceivedEventHandler,
};
use windows::{
    core::{Interface, HSTRING, PCWSTR, PWSTR},
    Win32::{
        Foundation::{E_POINTER, E_UNEXPECTED, HWND, LPARAM, LRESULT, RECT, WPARAM},
        System::LibraryLoader::GetModuleHandleW,
        UI::{
            Input::KeyboardAndMouse::{GetAsyncKeyState, SetFocus, VK_LBUTTON},
            Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass},
            WindowsAndMessaging::*,
        },
    },
};
use windows_core::BOOL;

/// Posted by the harness to the overlay root: hide now, show again (no activation) after 1 s.
const WM_REPRO_HIDE: u32 = WM_APP + 1;
/// Posted by the harness to the overlay root: return to the first shown position (no activation).
const WM_REPRO_RESET: u32 = WM_APP + 2;
const HIDE_TIMER: usize = 0x4D52;
const PARENT_SUBCLASS: usize = 0x5752_5950; // Wry's parent subclass stand-in
const NOACTIVATE_SUBCLASS: usize = 0x4D52_4E41; // Murmur's id ("MRNA")

static START: OnceLock<Instant> = OnceLock::new();
static LOG: OnceLock<Mutex<File>> = OnceLock::new();
static HOME: OnceLock<(i32, i32)> = OnceLock::new();
/// The overlay root; harness messages are honoured there only, never on in-thread children.
static OVERLAY_ROOT: OnceLock<usize> = OnceLock::new();

fn is_overlay_root(hwnd: HWND) -> bool {
    OVERLAY_ROOT.get() == Some(&(hwnd.0 as usize))
}

/// One JSON line: elapsed milliseconds, event name, then pre-formatted fields.
pub fn log(event: &str, fields: &str) {
    let elapsed = START.get_or_init(Instant::now).elapsed().as_secs_f64() * 1000.0;
    let line = if fields.is_empty() {
        format!(
            "{{\"t_ms\":{elapsed:.1},\"event\":{}}}\n",
            json_string(event)
        )
    } else {
        format!(
            "{{\"t_ms\":{elapsed:.1},\"event\":{},{fields}}}\n",
            json_string(event)
        )
    };
    if let Some(file) = LOG.get() {
        if let Ok(mut file) = file.lock() {
            let _ = file.write_all(line.as_bytes());
            let _ = file.flush();
        }
    }
}

fn hex(hwnd: HWND) -> String {
    json_string(&format!("{:#x}", hwnd.0 as usize))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Which {
    Overlay,
    Settings,
}

impl Which {
    fn name(self) -> &'static str {
        match self {
            Which::Overlay => "overlay",
            Which::Settings => "settings",
        }
    }
}

#[derive(Debug)]
enum UserEvent {
    Page(Which, &'static str),
}

struct Hosted {
    parent: HWND,
    host: HWND,
    controller: ICoreWebView2Controller,
}

thread_local! {
    static HOSTED: RefCell<Vec<Hosted>> = const { RefCell::new(Vec::new()) };
}

fn controller_for(parent: HWND) -> Option<(HWND, ICoreWebView2Controller)> {
    HOSTED.with(|hosted| {
        hosted
            .borrow()
            .iter()
            .find(|entry| entry.parent == parent)
            .map(|entry| (entry.host, entry.controller.clone()))
    })
}

const OVERLAY_HTML: &str = r#"<!doctype html><html><head><style>
html,body{margin:0;height:100%;background:transparent;overflow:hidden;user-select:none}
#pill{position:absolute;inset:4px;border-radius:12px;background:#335;cursor:grab}
</style></head><body><div id="pill" role="status" aria-label="Repro drag target"></div><script>
const post = m => window.chrome.webview.postMessage(m);
const pill = document.getElementById('pill');
pill.addEventListener('mousedown', e => { if (e.button === 0) { e.preventDefault(); post('down'); } });
window.addEventListener('mouseup', e => { if (e.button === 0) post('up'); });
pill.addEventListener('pointercancel', () => post('pointercancel'));
pill.addEventListener('lostpointercapture', () => post('lostpointercapture'));
post('ready');
</script></body></html>"#;

const SETTINGS_HTML: &str = r#"<!doctype html><html><body style="font:14px sans-serif;margin:24px">
<label>Repro field <input id="field" aria-label="Repro field" autocomplete="off"></label><script>
const post = m => window.chrome.webview.postMessage(m);
const field = document.getElementById('field');
field.addEventListener('focus', () => post('focus'));
field.addEventListener('blur', () => post('blur'));
post('ready');
</script></body></html>"#;

// ---------------------------------------------------------------- Wry-equivalent hosting

unsafe extern "system" fn host_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    // Wry's WRY_WEBVIEW window procedure: focus goes to the WebView2 document child.
    if msg == WM_SETFOCUS {
        if let Ok(child) = GetWindow(hwnd, GW_CHILD) {
            let _ = SetFocus(Some(child));
        }
    }
    DefWindowProcW(hwnd, msg, wparam, lparam)
}

fn client_rect(hwnd: HWND) -> RECT {
    let mut rect = RECT::default();
    unsafe {
        let _ = GetClientRect(hwnd, &mut rect);
    }
    rect
}

fn create_host(parent: HWND) -> windows::core::Result<HWND> {
    unsafe {
        let instance = GetModuleHandleW(PCWSTR::null())?;
        let class = windows::core::w!("WRY_WEBVIEW");
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            style: CS_HREDRAW | CS_VREDRAW,
            lpfnWndProc: Some(host_proc),
            hInstance: instance.into(),
            lpszClassName: class,
            ..Default::default()
        };
        RegisterClassExW(&wc); // a second registration fails harmlessly
        let rect = client_rect(parent);
        let host = CreateWindowExW(
            WINDOW_EX_STYLE::default(),
            class,
            PCWSTR::null(),
            WS_CHILD | WS_CLIPCHILDREN | WS_VISIBLE,
            0,
            0,
            rect.right - rect.left,
            rect.bottom - rect.top,
            Some(parent),
            None,
            Some(instance.into()),
            None,
        )?;
        SetWindowPos(
            host,
            Some(HWND_TOP),
            0,
            0,
            0,
            0,
            SWP_ASYNCWINDOWPOS | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOOWNERZORDER | SWP_NOSIZE,
        )?;
        Ok(host)
    }
}

/// Wry 0.57.2 `parent_subclass_proc`: bounds on size, focus forwarding, move notifications.
unsafe extern "system" fn parent_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    _data: usize,
) -> LRESULT {
    if let Some((host, controller)) = controller_for(hwnd) {
        match msg {
            WM_SIZE if wparam.0 != SIZE_MINIMIZED as usize => {
                let rect = client_rect(hwnd);
                let (width, height) = (rect.right - rect.left, rect.bottom - rect.top);
                let _ = controller.SetBounds(RECT {
                    left: 0,
                    top: 0,
                    right: width,
                    bottom: height,
                });
                let _ = SetWindowPos(
                    host,
                    None,
                    0,
                    0,
                    width,
                    height,
                    SWP_ASYNCWINDOWPOS | SWP_NOACTIVATE | SWP_NOZORDER,
                );
            }
            WM_SETFOCUS => {
                let _ = controller.MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);
            }
            WM_MOVE | WM_MOVING => {
                let _ = controller.NotifyParentWindowPositionChanged();
            }
            _ => {}
        }
    }
    if msg == WM_NCDESTROY {
        let _ = RemoveWindowSubclass(hwnd, Some(parent_proc), PARENT_SUBCLASS);
    }
    DefSubclassProc(hwnd, msg, wparam, lparam)
}

fn environment(data: &str) -> windows::core::Result<ICoreWebView2Environment> {
    let (tx, rx) = mpsc::channel();
    let options = CoreWebView2EnvironmentOptions::default();
    unsafe {
        options.set_additional_browser_arguments(
            "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection".to_string(),
        );
        CreateCoreWebView2EnvironmentWithOptions(
            PCWSTR::null(),
            &HSTRING::from(data),
            &ICoreWebView2EnvironmentOptions::from(options),
            &CreateCoreWebView2EnvironmentCompletedHandler::create(Box::new(move |error, env| {
                let result = error.and_then(|_| env.ok_or_else(|| E_POINTER.into()));
                tx.send(result)
                    .map_err(|_| windows::core::Error::from(E_UNEXPECTED))
            })),
        )?;
    }
    webview2_com::wait_with_pump(rx).map_err(|_| windows::core::Error::from(E_UNEXPECTED))?
}

fn controller(
    which: Which,
    host: HWND,
    env: &ICoreWebView2Environment,
    host_input: HostInput,
) -> windows::core::Result<ICoreWebView2Controller> {
    let (tx, rx) = mpsc::channel();
    let handler =
        CreateCoreWebView2ControllerCompletedHandler::create(Box::new(move |error, c| {
            let result = error.and_then(|_| c.ok_or_else(|| E_POINTER.into()));
            tx.send(result)
                .map_err(|_| windows::core::Error::from(E_UNEXPECTED))
        }));
    unsafe {
        let env10: ICoreWebView2Environment10 = env.cast()?;
        let options = env10.CreateCoreWebView2ControllerOptions()?;
        if which == Which::Overlay {
            // Tauri `transparent: true` -> Wry background (0,0,0,0) -> transparent default.
            if let Ok(options3) = options.cast::<ICoreWebView2ControllerOptions3>() {
                options3.SetDefaultBackgroundColor(COREWEBVIEW2_COLOR {
                    R: 0,
                    G: 0,
                    B: 0,
                    A: 0,
                })?;
            }
            let applied = match host_input {
                HostInput::Off => "not requested",
                HostInput::On => match options.cast::<ICoreWebView2ControllerOptions4>() {
                    Ok(options4) => match options4.SetAllowHostInputProcessing(true) {
                        Ok(()) => "enabled",
                        Err(_) => "set failed",
                    },
                    Err(_) => "unsupported runtime",
                },
            };
            log(
                "host_input",
                &format!(
                    "\"window\":\"overlay\",\"requested\":{},\"result\":{}",
                    json_string(if host_input == HostInput::On {
                        "on"
                    } else {
                        "off"
                    }),
                    json_string(applied)
                ),
            );
            if host_input == HostInput::On && applied != "enabled" {
                // An unsupported option must not be reported as a candidate result.
                return Err(E_UNEXPECTED.into());
            }
        }
        options.SetIsInPrivateModeEnabled(false)?;
        env10.CreateCoreWebView2ControllerWithOptions(host, &options, &handler)?;
    }
    webview2_com::wait_with_pump(rx).map_err(|_| windows::core::Error::from(E_UNEXPECTED))?
}

fn host_window(
    which: Which,
    window: &Window,
    env: &ICoreWebView2Environment,
    host_input: HostInput,
    proxy: EventLoopProxy<UserEvent>,
) -> windows::core::Result<()> {
    let parent = HWND(window.hwnd() as _);
    let host = create_host(parent)?;
    let controller = controller(which, host, env, host_input)?;
    unsafe {
        let rect = client_rect(parent);
        controller.SetBounds(RECT {
            left: 0,
            top: 0,
            right: rect.right - rect.left,
            bottom: rect.bottom - rect.top,
        })?;
        controller.SetIsVisible(true)?;
        let webview = controller.CoreWebView2()?;
        // Wry 0.57.2 always enables non-client region support; it changes hit-testing.
        if let Ok(settings9) = webview.Settings()?.cast::<ICoreWebView2Settings9>() {
            settings9.SetIsNonClientRegionSupportEnabled(true)?;
        }
        let mut token = 0i64;
        webview.add_WebMessageReceived(
            &WebMessageReceivedEventHandler::create(Box::new(move |_, args| {
                if let Some(args) = args {
                    let mut raw = PWSTR::null();
                    if args.TryGetWebMessageAsString(&mut raw).is_ok() {
                        let message = page_message(&take_pwstr(raw));
                        let _ = proxy.send_event(UserEvent::Page(which, message));
                    }
                }
                Ok(())
            })),
            &mut token,
        )?;
        webview.NavigateToString(&HSTRING::from(match which {
            Which::Overlay => OVERLAY_HTML,
            Which::Settings => SETTINGS_HTML,
        }))?;
        HOSTED.with(|hosted| {
            hosted.borrow_mut().push(Hosted {
                parent,
                host,
                controller,
            })
        });
        if !SetWindowSubclass(parent, Some(parent_proc), PARENT_SUBCLASS, 0).as_bool() {
            return Err(E_UNEXPECTED.into());
        }
        log(
            "hosted",
            &format!(
                "\"window\":{},\"root\":{},\"host\":{}",
                json_string(which.name()),
                hex(parent),
                hex(host)
            ),
        );
    }
    Ok(())
}

// ---------------------------------------------------------------- Murmur's overlay subclass

unsafe extern "system" fn noactivate_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    _data: usize,
) -> LRESULT {
    match msg {
        WM_MOUSEACTIVATE => {
            log("wm_mouseactivate", &format!("\"hwnd\":{}", hex(hwnd)));
            return LRESULT(MA_NOACTIVATE as isize);
        }
        WM_ACTIVATE => {
            let previous = HWND(lparam.0 as _);
            let valid = !previous.is_invalid() && IsWindow(Some(previous)).as_bool();
            let state = wparam.0 & 0xFFFF;
            log(
                "wm_activate",
                &format!(
                    "\"hwnd\":{},\"state\":{state},\"previous\":{},\"previous_valid\":{valid}",
                    hex(hwnd),
                    hex(previous)
                ),
            );
            if hand_back(wparam.0, valid) {
                let succeeded = SetForegroundWindow(previous).as_bool();
                log("handback", &format!("\"succeeded\":{succeeded}"));
                return LRESULT(0);
            }
        }
        WM_ENTERSIZEMOVE => log("enter_size_move", &format!("\"hwnd\":{}", hex(hwnd))),
        WM_EXITSIZEMOVE => log("exit_size_move", &format!("\"hwnd\":{}", hex(hwnd))),
        WM_CAPTURECHANGED => log(
            "capture_changed",
            &format!(
                "\"hwnd\":{},\"new\":{}",
                hex(hwnd),
                hex(HWND(lparam.0 as _))
            ),
        ),
        WM_REPRO_HIDE if is_overlay_root(hwnd) => {
            let _ = ShowWindow(hwnd, SW_HIDE);
            SetTimer(Some(hwnd), HIDE_TIMER, 1000, None);
            log("hidden", "");
            return LRESULT(0);
        }
        WM_REPRO_RESET if is_overlay_root(hwnd) => {
            if let Some(&(x, y)) = HOME.get() {
                let _ = SetWindowPos(
                    hwnd,
                    None,
                    x,
                    y,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                );
                log("reset", &format!("\"x\":{x},\"y\":{y}"));
            }
            return LRESULT(0);
        }
        WM_TIMER if wparam.0 == HIDE_TIMER && is_overlay_root(hwnd) => {
            let _ = KillTimer(Some(hwnd), HIDE_TIMER);
            let _ = ShowWindow(hwnd, SW_SHOWNOACTIVATE);
            log("shown", "");
            return LRESULT(0);
        }
        WM_NCDESTROY => {
            let _ = RemoveWindowSubclass(hwnd, Some(noactivate_proc), NOACTIVATE_SUBCLASS);
        }
        _ => {}
    }
    DefSubclassProc(hwnd, msg, wparam, lparam)
}

unsafe extern "system" fn subclass_owned_child(hwnd: HWND, count: LPARAM) -> BOOL {
    let mut process = 0u32;
    let thread = GetWindowThreadProcessId(hwnd, Some(&mut process));
    if process == std::process::id()
        && thread == windows::Win32::System::Threading::GetCurrentThreadId()
        && SetWindowSubclass(hwnd, Some(noactivate_proc), NOACTIVATE_SUBCLASS, 0).as_bool()
    {
        *(count.0 as *mut usize) += 1;
    }
    BOOL(1)
}

/// Murmur's `noactivate::install`: the root plus every descendant this thread owns.
fn install_noactivate(root: HWND) {
    let mut children = 0usize;
    unsafe {
        let top = SetWindowSubclass(root, Some(noactivate_proc), NOACTIVATE_SUBCLASS, 0).as_bool();
        let _ = EnumChildWindows(
            Some(root),
            Some(subclass_owned_child),
            LPARAM(&mut children as *mut usize as isize),
        );
        log(
            "noactivate_installed",
            &format!("\"top_level\":{top},\"children\":{children}"),
        );
    }
}

// ---------------------------------------------------------------- event loop

pub fn run() -> Result<(), String> {
    START.get_or_init(Instant::now);
    let args = parse_args(&std::env::args().skip(1).collect::<Vec<_>>())?;
    LOG.set(Mutex::new(
        File::create(&args.log).map_err(|e| format!("log: {e}"))?,
    ))
    .map_err(|_| "log already set")?;
    log(
        "start",
        &format!(
            "\"pid\":{},\"host_input\":{}",
            std::process::id(),
            json_string(if args.host_input == HostInput::On {
                "on"
            } else {
                "off"
            })
        ),
    );

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let settings = WindowBuilder::new()
        .with_title("WebView2 Repro Settings")
        .with_inner_size(LogicalSize::new(600.0, 400.0))
        .build(&event_loop)
        .map_err(|e| e.to_string())?;
    let overlay = WindowBuilder::new()
        .with_title("WebView2 Repro Overlay")
        .with_inner_size(LogicalSize::new(120.0, 80.0))
        .with_decorations(false)
        .with_transparent(true)
        .with_always_on_top(true)
        .with_resizable(false)
        .with_focusable(false)
        .with_skip_taskbar(true)
        .with_undecorated_shadow(false) // Murmur: "shadow": false
        .with_visible(false)
        .build(&event_loop)
        .map_err(|e| e.to_string())?;
    if let Some(monitor) = settings.current_monitor() {
        let size = monitor.size();
        let origin = monitor.position();
        overlay.set_outer_position(PhysicalPosition::new(
            origin.x + size.width as i32 / 2 - 60,
            origin.y + size.height as i32 - 240,
        ));
    }

    let env = environment(&args.data).map_err(|e| format!("environment: {e}"))?;
    log("environment", "");
    host_window(
        Which::Settings,
        &settings,
        &env,
        HostInput::Off,
        event_loop.create_proxy(),
    )
    .map_err(|e| format!("settings webview: {e}"))?;
    host_window(
        Which::Overlay,
        &overlay,
        &env,
        args.host_input,
        event_loop.create_proxy(),
    )
    .map_err(|e| format!("overlay webview: {e}"))?;

    let overlay_root = HWND(overlay.hwnd() as _);
    let _ = OVERLAY_ROOT.set(overlay_root.0 as usize);
    let (overlay_id, settings_id) = (overlay.id(), settings.id());
    let mut overlay_shown = false;
    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(UserEvent::Page(which, message)) => {
                log(
                    "page",
                    &format!(
                        "\"window\":{},\"message\":{}",
                        json_string(which.name()),
                        json_string(message)
                    ),
                );
                match (which, message) {
                    (Which::Overlay, "ready") if !overlay_shown => {
                        overlay_shown = true;
                        overlay.set_visible(true);
                        install_noactivate(overlay_root);
                        let mut rect = RECT::default();
                        if unsafe { GetWindowRect(overlay_root, &mut rect) }.is_ok() {
                            let _ = HOME.set((rect.left, rect.top));
                        }
                        log("overlay_shown", &format!("\"root\":{}", hex(overlay_root)));
                    }
                    (Which::Overlay, "down") => {
                        // start_overlay_drag refuses when the button was already released.
                        let held =
                            unsafe { GetAsyncKeyState(VK_LBUTTON.0 as i32) } as u16 & 0x8000 != 0;
                        if !held {
                            log(
                                "drag_refused",
                                "\"reason\":\"release received before drag started\"",
                            );
                        } else {
                            let result = overlay.drag_window();
                            log("drag_engaged", &format!("\"ok\":{}", result.is_ok()));
                        }
                    }
                    _ => {}
                }
            }
            Event::WindowEvent {
                window_id,
                event: WindowEvent::Moved(position),
                ..
            } if window_id == overlay_id => {
                log(
                    "moved",
                    &format!("\"x\":{},\"y\":{}", position.x, position.y),
                );
            }
            Event::WindowEvent {
                window_id,
                event: WindowEvent::Focused(focused),
                ..
            } => {
                let which = if window_id == overlay_id {
                    "overlay"
                } else {
                    "settings"
                };
                log(
                    "tao_focused",
                    &format!("\"window\":\"{which}\",\"focused\":{focused}"),
                );
            }
            Event::WindowEvent {
                window_id,
                event: WindowEvent::CloseRequested,
                ..
            } if window_id == settings_id => {
                log("exit", "");
                *flow = ControlFlow::Exit;
            }
            _ => {}
        }
    });
}
