//! The Mac's physical keys as `send_input` key names (docs/control-api.md, "Key names"). Both follow the W3C UI Events
//! `code` values, so the names are the same except winit's `SuperLeft`/`SuperRight` (the Command keys), which the
//! standard and natemu call `MetaLeft`/`MetaRight`. A key natemu has no name for (F13, media keys, ...) is `None`.
use winit::keyboard::KeyCode;

macro_rules! same_names {
    ($code:expr, { $($k:ident),* $(,)? }, { $($from:ident => $to:literal),* $(,)? }) => {
        match $code {
            $(KeyCode::$k => Some(stringify!($k)),)*
            $(KeyCode::$from => Some($to),)*
            _ => None,
        }
    };
}

pub fn name(code: KeyCode) -> Option<&'static str> {
    same_names!(code, {
        KeyA, KeyB, KeyC, KeyD, KeyE, KeyF, KeyG, KeyH, KeyI, KeyJ, KeyK, KeyL, KeyM, KeyN, KeyO, KeyP, KeyQ, KeyR,
        KeyS, KeyT, KeyU, KeyV, KeyW, KeyX, KeyY, KeyZ,
        Digit0, Digit1, Digit2, Digit3, Digit4, Digit5, Digit6, Digit7, Digit8, Digit9,
        Enter, Escape, Backspace, Tab, Space, Minus, Equal, BracketLeft, BracketRight, Backslash, Semicolon, Quote,
        Backquote, Comma, Period, Slash, CapsLock,
        ShiftLeft, ShiftRight, ControlLeft, ControlRight, AltLeft, AltRight, ContextMenu,
        F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12, PrintScreen, ScrollLock, Pause,
        Insert, Home, PageUp, Delete, End, PageDown, ArrowRight, ArrowLeft, ArrowDown, ArrowUp,
        NumLock, NumpadDivide, NumpadMultiply, NumpadSubtract, NumpadAdd, NumpadEnter,
        Numpad0, Numpad1, Numpad2, Numpad3, Numpad4, Numpad5, Numpad6, Numpad7, Numpad8, Numpad9, NumpadDecimal,
        IntlBackslash, IntlRo, IntlYen,
    }, {
        SuperLeft => "MetaLeft",
        SuperRight => "MetaRight",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// docs/control-api.md's key names, as the page lists them.
    const DOCUMENTED: &str = "KeyA KeyB KeyC KeyD KeyE KeyF KeyG KeyH KeyI KeyJ KeyK KeyL KeyM KeyN KeyO KeyP KeyQ KeyR \
        KeyS KeyT KeyU KeyV KeyW KeyX KeyY KeyZ Digit0 Digit1 Digit2 Digit3 Digit4 Digit5 Digit6 Digit7 Digit8 Digit9 \
        Enter Escape Backspace Tab Space Minus Equal BracketLeft BracketRight Backslash Semicolon Quote Backquote Comma \
        Period Slash CapsLock ShiftLeft ShiftRight ControlLeft ControlRight AltLeft AltRight MetaLeft MetaRight \
        ContextMenu F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F12 PrintScreen ScrollLock Pause Insert Home PageUp Delete End \
        PageDown ArrowRight ArrowLeft ArrowDown ArrowUp NumLock NumpadDivide NumpadMultiply NumpadSubtract NumpadAdd \
        NumpadEnter Numpad0 Numpad1 Numpad2 Numpad3 Numpad4 Numpad5 Numpad6 Numpad7 Numpad8 Numpad9 NumpadDecimal \
        IntlBackslash IntlRo IntlYen";

    #[test]
    fn maps_onto_the_documented_names() {
        assert_eq!(name(KeyCode::KeyA), Some("KeyA"));
        assert_eq!(name(KeyCode::SuperLeft), Some("MetaLeft"));
        assert_eq!(name(KeyCode::F13), None);
        let documented: Vec<&str> = DOCUMENTED.split_whitespace().collect();
        // Every code the viewer maps lands on a documented name, and every documented name is reached by one code.
        let mapped: Vec<&str> = all_codes().into_iter().filter_map(name).collect();
        for m in &mapped {
            assert!(documented.contains(m), "{m}: not a documented key name");
        }
        for d in &documented {
            assert_eq!(mapped.iter().filter(|m| *m == d).count(), 1, "{d}: not reached by exactly one key");
        }
    }

    fn all_codes() -> Vec<KeyCode> {
        use KeyCode::*;
        vec![
            KeyA, KeyB, KeyC, KeyD, KeyE, KeyF, KeyG, KeyH, KeyI, KeyJ, KeyK, KeyL, KeyM, KeyN, KeyO, KeyP, KeyQ, KeyR,
            KeyS, KeyT, KeyU, KeyV, KeyW, KeyX, KeyY, KeyZ, Digit0, Digit1, Digit2, Digit3, Digit4, Digit5, Digit6,
            Digit7, Digit8, Digit9, Enter, Escape, Backspace, Tab, Space, Minus, Equal, BracketLeft, BracketRight,
            Backslash, Semicolon, Quote, Backquote, Comma, Period, Slash, CapsLock, ShiftLeft, ShiftRight, ControlLeft,
            ControlRight, AltLeft, AltRight, SuperLeft, SuperRight, ContextMenu, F1, F2, F3, F4, F5, F6, F7, F8, F9,
            F10, F11, F12, PrintScreen, ScrollLock, Pause, Insert, Home, PageUp, Delete, End, PageDown, ArrowRight,
            ArrowLeft, ArrowDown, ArrowUp, NumLock, NumpadDivide, NumpadMultiply, NumpadSubtract, NumpadAdd,
            NumpadEnter, Numpad0, Numpad1, Numpad2, Numpad3, Numpad4, Numpad5, Numpad6, Numpad7, Numpad8, Numpad9,
            NumpadDecimal, IntlBackslash, IntlRo, IntlYen, F13, Fn, MediaPlayPause, NumpadEqual,
        ]
    }
}
