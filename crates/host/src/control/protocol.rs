//! The control API's wire format (`docs/restructure/control-api-plan.md`, Design): JSON-RPC 2.0, one compact JSON value
//! per line. Decoding is two-stage, so every failure keeps the request's `id` and gets its own code: first the
//! envelope (`jsonrpc`, `id`, `method`, `params`; a failure is -32700 or -32600), then `method` (unknown: -32601) and its
//! parameters (-32602, with serde's message in `data`). Wire names are stable: renaming a method, field or key code is a
//! breaking change; additions are not.
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The protocol version `query_status` reports.
pub const PROTOCOL: u32 = 1;

/// A decoded method call.
#[derive(Debug, Clone, PartialEq)]
pub enum Call {
    SendInput(SendInput),
    PowerButton,
    Quit,
    QueryStatus,
}

/// `send_input`'s parameters: the events, applied in order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SendInput {
    pub events: Vec<InputEvent>,
}

/// One input event. `dy` is positive downward (the screen's convention) for both the pointer and the wheel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum InputEvent {
    Key { code: KeyCode, pressed: bool },
    PointerMotion { dx: i32, dy: i32 },
    Button { button: MouseButton, pressed: bool },
    Scroll { dy: i32 },
}

/// A mouse button (wire `"left"`, `"right"`, `"middle"`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MouseButton {
    Left,
    Right,
    Middle,
}

macro_rules! key_codes {
    ($($name:ident),* $(,)?) => {
        /// A key, named by its W3C UI Events KeyboardEvent `code` value (w3.org/TR/uievents-code), which is also its
        /// wire name: the 104-key US layout and the other keys with a set 2 code (`crate::control::ps2`).
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        pub enum KeyCode { $($name),* }
        impl KeyCode {
            /// Every key code, in declaration order.
            pub const ALL: &'static [KeyCode] = &[$(KeyCode::$name),*];
        }
    };
}

key_codes! {
    // Letters and digits.
    KeyA, KeyB, KeyC, KeyD, KeyE, KeyF, KeyG, KeyH, KeyI, KeyJ, KeyK, KeyL, KeyM,
    KeyN, KeyO, KeyP, KeyQ, KeyR, KeyS, KeyT, KeyU, KeyV, KeyW, KeyX, KeyY, KeyZ,
    Digit1, Digit2, Digit3, Digit4, Digit5, Digit6, Digit7, Digit8, Digit9, Digit0,
    // The rest of the alphanumeric section.
    Enter, Escape, Backspace, Tab, Space, Minus, Equal, BracketLeft, BracketRight, Backslash, Semicolon, Quote,
    Backquote, Comma, Period, Slash, CapsLock,
    ShiftLeft, ShiftRight, ControlLeft, ControlRight, AltLeft, AltRight, MetaLeft, MetaRight, ContextMenu,
    // Function row.
    F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12, PrintScreen, ScrollLock, Pause,
    // Control pad and arrows.
    Insert, Home, PageUp, Delete, End, PageDown, ArrowRight, ArrowLeft, ArrowDown, ArrowUp,
    // Numpad.
    NumLock, NumpadDivide, NumpadMultiply, NumpadSubtract, NumpadAdd, NumpadEnter,
    Numpad1, Numpad2, Numpad3, Numpad4, Numpad5, Numpad6, Numpad7, Numpad8, Numpad9, Numpad0, NumpadDecimal,
    // Keys beyond the US layout.
    IntlBackslash, IntlRo, IntlYen,
}

/// The error codes: JSON-RPC's, then natemu's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorCode {
    ParseError,
    InvalidRequest,
    MethodNotFound,
    InvalidParams,
    /// The device the request needs is absent.
    NoDevice,
    /// The run is stopping: the request was not applied.
    Stopping,
    /// The guest stopped taking input; the events not yet delivered were discarded.
    InputStalled,
}

impl ErrorCode {
    pub fn code(self) -> i64 {
        match self {
            ErrorCode::ParseError => -32700,
            ErrorCode::InvalidRequest => -32600,
            ErrorCode::MethodNotFound => -32601,
            ErrorCode::InvalidParams => -32602,
            ErrorCode::NoDevice => -32001,
            ErrorCode::Stopping => -32002,
            ErrorCode::InputStalled => -32003,
        }
    }
    fn message(self) -> &'static str {
        match self {
            ErrorCode::ParseError => "Parse error",
            ErrorCode::InvalidRequest => "Invalid Request",
            ErrorCode::MethodNotFound => "Method not found",
            ErrorCode::InvalidParams => "Invalid params",
            ErrorCode::NoDevice => "Device absent",
            ErrorCode::Stopping => "The run is stopping",
            ErrorCode::InputStalled => "The guest stopped taking input",
        }
    }
}

/// An error response's body.
#[derive(Debug, Clone, PartialEq)]
pub struct Error {
    pub code: ErrorCode,
    pub message: String,
    pub data: Option<Value>,
}

impl Error {
    /// The code's standard message, no data.
    pub fn new(code: ErrorCode) -> Self {
        Error { code, message: code.message().into(), data: None }
    }
    pub fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }
    /// -32001 for an absent `device` (`data: {"device": ...}`).
    pub fn no_device(device: &str) -> Self {
        Error::new(ErrorCode::NoDevice).with_data(serde_json::json!({ "device": device }))
    }
}

/// A decoded request; `id` is absent for a notification (never answered).
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    pub id: Option<Value>,
    pub call: Call,
}

/// A request that failed to decode, with the `id` it had (if any could be read).
#[derive(Debug, Clone, PartialEq)]
pub struct Rejected {
    pub id: Option<Value>,
    pub error: Error,
}

/// A response: a result or an error, for `id` (null when the request's could not be read).
#[derive(Debug, Clone, PartialEq)]
pub struct Response {
    pub id: Value,
    pub outcome: Result<Value, Error>,
}

/// One line from a client: a single request or a batch (each element decoded on its own).
#[derive(Debug, Clone, PartialEq)]
pub enum Incoming {
    Single(Result<Request, Rejected>),
    Batch(Vec<Result<Request, Rejected>>),
}

/// Why the run stopped (the `stopped` notification's `reason`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    PowerOff,
    Reset,
    Panic,
    Cancelled,
    Error,
}

/// Decodes one line: -32700 if it is not JSON, an empty batch -32600.
pub fn decode_line(line: &str) -> Incoming {
    match serde_json::from_str::<Value>(line) {
        Err(e) => Incoming::Single(Err(Rejected::new(None, ErrorCode::ParseError).with_data(e))),
        Ok(Value::Array(batch)) if batch.is_empty() => Incoming::Single(Err(Rejected::new(None, ErrorCode::InvalidRequest))),
        Ok(Value::Array(batch)) => Incoming::Batch(batch.into_iter().map(Request::decode).collect()),
        Ok(value) => Incoming::Single(Request::decode(value)),
    }
}

/// The envelope's members.
const MEMBERS: [&str; 4] = ["jsonrpc", "id", "method", "params"];

impl Request {
    /// Decodes one request value (two-stage, see the module documentation).
    pub fn decode(value: Value) -> Result<Request, Rejected> {
        let Value::Object(mut fields) = value else {
            return Err(Rejected::new(None, ErrorCode::InvalidRequest));
        };
        // Stage 1: the envelope. The id first, so every later failure keeps it.
        let id = match fields.remove("id") {
            None => None,
            Some(id @ (Value::Null | Value::Number(_) | Value::String(_))) => Some(id),
            Some(_) => return Err(Rejected::new(None, ErrorCode::InvalidRequest).with_data("id: a string, number or null")),
        };
        let invalid = |why: &str| Err(Rejected::new(id.clone(), ErrorCode::InvalidRequest).with_data(why));
        if fields.get("jsonrpc") != Some(&Value::from("2.0")) {
            return invalid("jsonrpc: \"2.0\"");
        }
        let Some(Value::String(method)) = fields.remove("method") else {
            return invalid("method: a string");
        };
        let params = fields.remove("params");
        if !matches!(params, None | Some(Value::Object(_) | Value::Array(_))) {
            return invalid("params: an object or an array");
        }
        if let Some(extra) = fields.keys().find(|k| !MEMBERS.contains(&k.as_str())) {
            return invalid(&format!("unknown member {extra}"));
        }
        // Stage 2: the method and its parameters.
        let bad_params = |why: String| Rejected::new(id.clone(), ErrorCode::InvalidParams).with_data(why);
        let none = |call: Call| match &params {
            None => Ok(call),
            Some(Value::Object(m)) if m.is_empty() => Ok(call),
            Some(Value::Array(a)) if a.is_empty() => Ok(call),
            Some(_) => Err(bad_params(format!("{method} takes no parameters"))),
        };
        let call = match method.as_str() {
            // By name only: serde would also take a struct from an array (by position).
            "send_input" => match params.clone() {
                Some(p @ Value::Object(_)) => match serde_json::from_value::<SendInput>(p) {
                    Ok(p) => Call::SendInput(p),
                    Err(e) => return Err(bad_params(e.to_string())),
                },
                _ => return Err(bad_params("send_input takes params {\"events\": [...]}".into())),
            },
            "power_button" => none(Call::PowerButton)?,
            "quit" => none(Call::Quit)?,
            "query_status" => none(Call::QueryStatus)?,
            _ => return Err(Rejected::new(id, ErrorCode::MethodNotFound)),
        };
        Ok(Request { id, call })
    }
    /// The response to this request with `outcome`, none for a notification.
    pub fn answer(&self, outcome: Result<Value, Error>) -> Option<Response> {
        self.id.clone().map(|id| Response { id, outcome })
    }
}

impl Rejected {
    /// The error response: a parse error or an invalid request is always answered (with a null id when none could be
    /// read); a notification's other errors are not.
    pub fn response(self) -> Option<Response> {
        let always = matches!(self.error.code, ErrorCode::ParseError | ErrorCode::InvalidRequest);
        let id = if always { Some(self.id.unwrap_or(Value::Null)) } else { self.id };
        id.map(|id| Response { id, outcome: Err(self.error) })
    }
    fn new(id: Option<Value>, code: ErrorCode) -> Self {
        Rejected { id, error: Error::new(code) }
    }
    fn with_data(mut self, data: impl std::fmt::Display) -> Self {
        self.error.data = Some(Value::String(data.to_string()));
        self
    }
}

impl Response {
    /// The response as one compact JSON line, without the newline.
    pub fn to_line(&self) -> String {
        #[derive(Serialize)]
        struct Wire<'a> {
            jsonrpc: &'static str,
            id: &'a Value,
            #[serde(skip_serializing_if = "Option::is_none")]
            result: Option<&'a Value>,
            #[serde(skip_serializing_if = "Option::is_none")]
            error: Option<WireError<'a>>,
        }
        #[derive(Serialize)]
        struct WireError<'a> {
            code: i64,
            message: &'a str,
            #[serde(skip_serializing_if = "Option::is_none")]
            data: Option<&'a Value>,
        }
        let (result, error) = match &self.outcome {
            Ok(v) => (Some(v), None),
            Err(e) => (None, Some(WireError { code: e.code.code(), message: &e.message, data: e.data.as_ref() })),
        };
        serde_json::to_string(&Wire { jsonrpc: "2.0", id: &self.id, result, error }).expect("a response serializes")
    }
}

/// The `stopped` notification as one compact JSON line, without the newline.
pub fn stopped(reason: StopReason) -> String {
    #[derive(Serialize)]
    struct Params {
        reason: StopReason,
    }
    #[derive(Serialize)]
    struct Wire {
        jsonrpc: &'static str,
        method: &'static str,
        params: Params,
    }
    serde_json::to_string(&Wire { jsonrpc: "2.0", method: "stopped", params: Params { reason } })
        .expect("a notification serializes")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request(line: &str) -> Result<Request, Rejected> {
        match decode_line(line) {
            Incoming::Single(r) => r,
            Incoming::Batch(_) => panic!("a batch: {line}"),
        }
    }

    /// The Design section's three event examples round-trip byte for byte, and its request example decodes to them.
    #[test]
    fn wire_names_match_examples() {
        let examples = [
            r#"{"type":"key","code":"KeyA","pressed":true}"#,
            r#"{"type":"pointer_motion","dx":10,"dy":-3}"#,
            r#"{"type":"button","button":"left","pressed":true}"#,
        ];
        let events = [
            InputEvent::Key { code: KeyCode::KeyA, pressed: true },
            InputEvent::PointerMotion { dx: 10, dy: -3 },
            InputEvent::Button { button: MouseButton::Left, pressed: true },
        ];
        for (text, event) in examples.iter().zip(events) {
            assert_eq!(serde_json::from_str::<InputEvent>(text).unwrap(), event, "{text}");
            assert_eq!(serde_json::to_string(&event).unwrap(), *text);
        }
        let line = format!(r#"{{"jsonrpc":"2.0","id":1,"method":"send_input","params":{{"events":[{}]}}}}"#, examples.join(","));
        let r = request(&line).unwrap();
        assert_eq!(r, Request { id: Some(json!(1)), call: Call::SendInput(SendInput { events: events.to_vec() }) });
        assert_eq!(r.answer(Ok(json!({}))).unwrap().to_line(), r#"{"jsonrpc":"2.0","id":1,"result":{}}"#);
        let scroll = r#"{"type":"scroll","dy":2}"#;
        assert_eq!(serde_json::from_str::<InputEvent>(scroll).unwrap(), InputEvent::Scroll { dy: 2 });
        for (m, call) in [("power_button", Call::PowerButton), ("quit", Call::Quit), ("query_status", Call::QueryStatus)] {
            assert_eq!(request(&format!(r#"{{"jsonrpc":"2.0","id":2,"method":"{m}"}}"#)).unwrap().call, call);
        }
        assert_eq!(
            stopped(StopReason::PowerOff),
            r#"{"jsonrpc":"2.0","method":"stopped","params":{"reason":"power_off"}}"#
        );
    }

    /// Methods without parameters take `params` absent, `{}` or `[]`; anything else is -32602.
    #[test]
    fn params_may_be_omitted() {
        for params in ["", r#","params":{}"#, r#","params":[]"#] {
            let line = format!(r#"{{"jsonrpc":"2.0","id":3,"method":"quit"{params}}}"#);
            assert_eq!(request(&line), Ok(Request { id: Some(json!(3)), call: Call::Quit }), "{line}");
        }
        for params in [r#"{"now":true}"#, "[1]"] {
            let line = format!(r#"{{"jsonrpc":"2.0","id":3,"method":"quit","params":{params}}}"#);
            assert_eq!(request(&line).unwrap_err().error.code, ErrorCode::InvalidParams, "{line}");
        }
        // A notification (no id) decodes too.
        assert_eq!(request(r#"{"jsonrpc":"2.0","method":"quit"}"#), Ok(Request { id: None, call: Call::Quit }));
    }

    /// Each decoding failure gets its own code and keeps the request's id; a parse error or an invalid request without
    /// a readable id is answered with a null id; a notification's method or params error is not answered.
    #[test]
    fn decode_errors_keep_the_id() {
        let e = request(r#"{"jsonrpc":"2.0","id":7,"method":"reboot"}"#).unwrap_err();
        assert_eq!((e.id.clone(), e.error.code), (Some(json!(7)), ErrorCode::MethodNotFound));
        assert_eq!(
            e.response().unwrap().to_line(),
            r#"{"jsonrpc":"2.0","id":7,"error":{"code":-32601,"message":"Method not found"}}"#
        );
        let bad = [
            r#"{"jsonrpc":"2.0","id":"x","method":"send_input","params":{"events":[{"type":"key","code":"NoSuchKey","pressed":true}]}}"#,
            r#"{"jsonrpc":"2.0","id":"x","method":"send_input","params":{"events":[{"type":"key","code":"KeyA"}]}}"#,
            r#"{"jsonrpc":"2.0","id":"x","method":"send_input","params":{"events":[],"extra":1}}"#,
            r#"{"jsonrpc":"2.0","id":"x","method":"send_input","params":{"events":[{"type":"scroll","dy":1,"dx":1}]}}"#,
            r#"{"jsonrpc":"2.0","id":"x","method":"send_input"}"#,
            r#"{"jsonrpc":"2.0","id":"x","method":"send_input","params":[[]]}"#,
        ];
        for line in bad {
            let e = request(line).unwrap_err();
            assert_eq!((e.id.clone(), e.error.code), (Some(json!("x")), ErrorCode::InvalidParams), "{line}");
            assert!(e.error.data.is_some(), "serde's message in data: {line}");
            let v: Value = serde_json::from_str(&e.response().unwrap().to_line()).unwrap();
            assert_eq!((&v["id"], &v["error"]["code"]), (&json!("x"), &json!(-32602)));
        }
        let invalid = [
            (r#"{"jsonrpc":"1.0","id":4,"method":"quit"}"#, Some(json!(4))),
            (r#"{"jsonrpc":"2.0","id":4,"method":5}"#, Some(json!(4))),
            (r#"{"jsonrpc":"2.0","id":4}"#, Some(json!(4))),
            (r#"{"jsonrpc":"2.0","id":4,"method":"quit","params":3}"#, Some(json!(4))),
            (r#"{"jsonrpc":"2.0","id":4,"method":"quit","extra":0}"#, Some(json!(4))),
            (r#"{"jsonrpc":"2.0","id":{"a":1},"method":"quit"}"#, None),
            (r#"[1,2]"#, None),
            (r#"5"#, None),
        ];
        for (line, id) in invalid {
            let rejected: Vec<_> = match decode_line(line) {
                Incoming::Single(r) => vec![r.unwrap_err()],
                Incoming::Batch(b) => b.into_iter().map(|r| r.unwrap_err()).collect(),
            };
            for e in rejected {
                assert_eq!((e.id.clone(), e.error.code), (id.clone(), ErrorCode::InvalidRequest), "{line}");
                assert_eq!(e.response().unwrap().id, id.clone().unwrap_or(Value::Null), "{line}");
            }
        }
        let e = request("{not json").unwrap_err();
        assert_eq!((e.id.clone(), e.error.code), (None, ErrorCode::ParseError));
        assert_eq!(
            serde_json::from_str::<Value>(&e.response().unwrap().to_line()).unwrap()["id"],
            Value::Null
        );
        assert_eq!(request("[]").unwrap_err().error.code, ErrorCode::InvalidRequest, "an empty batch");
        // Notifications: an unknown method or bad params is not answered.
        assert_eq!(request(r#"{"jsonrpc":"2.0","method":"reboot"}"#).unwrap_err().response(), None);
        assert_eq!(request(r#"{"jsonrpc":"2.0","method":"quit","params":[1]}"#).unwrap_err().response(), None);
        // A batch: each element on its own.
        match decode_line(r#"[{"jsonrpc":"2.0","id":1,"method":"quit"},{"jsonrpc":"2.0","id":2,"method":"nope"}]"#) {
            Incoming::Batch(b) => {
                assert_eq!(b[0], Ok(Request { id: Some(json!(1)), call: Call::Quit }));
                assert_eq!(b[1].as_ref().unwrap_err().error.code, ErrorCode::MethodNotFound);
            }
            other => panic!("{other:?}"),
        }
        // natemu's codes.
        assert_eq!(
            [ErrorCode::NoDevice, ErrorCode::Stopping, ErrorCode::InputStalled].map(ErrorCode::code),
            [-32001, -32002, -32003]
        );
        assert_eq!(
            Response { id: json!(9), outcome: Err(Error::no_device("ps2")) }.to_line(),
            r#"{"jsonrpc":"2.0","id":9,"error":{"code":-32001,"message":"Device absent","data":{"device":"ps2"}}}"#
        );
    }
}
