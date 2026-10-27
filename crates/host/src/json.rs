//! Minimal JSON reader for tool output (fexc's report). Numbers stay text.
#[derive(Debug, PartialEq)]
pub(crate) enum Value {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}
impl Value {
    pub(crate) fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(members) => members.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub(crate) fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(s) => Some(s),
            _ => None,
        }
    }
}

pub(crate) fn parse(text: &str) -> Result<Value, String> {
    let mut p = Parser { b: text.as_bytes(), i: 0 };
    let v = p.value()?;
    p.space();
    if p.i != p.b.len() {
        return Err(format!("JSON: trailing data at {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}
impl Parser<'_> {
    fn space(&mut self) {
        while self.b.get(self.i).is_some_and(|c| c.is_ascii_whitespace()) {
            self.i += 1;
        }
    }
    fn fail<T>(&self) -> Result<T, String> {
        Err(format!("JSON: unexpected input at {}", self.i))
    }
    fn eat(&mut self, c: u8) -> bool {
        self.space();
        let hit = self.b.get(self.i) == Some(&c);
        self.i += usize::from(hit);
        hit
    }
    fn value(&mut self) -> Result<Value, String> {
        self.space();
        let rest = &self.b[self.i..];
        for (word, v) in [("null", Value::Null), ("true", Value::Bool(true)), ("false", Value::Bool(false))] {
            if rest.starts_with(word.as_bytes()) {
                self.i += word.len();
                return Ok(v);
            }
        }
        match rest.first() {
            Some(b'"') => Ok(Value::String(self.string()?)),
            Some(b'[') => {
                self.i += 1;
                let mut items = Vec::new();
                if !self.eat(b']') {
                    loop {
                        items.push(self.value()?);
                        if self.eat(b']') {
                            break;
                        }
                        if !self.eat(b',') {
                            return self.fail();
                        }
                    }
                }
                Ok(Value::Array(items))
            }
            Some(b'{') => {
                self.i += 1;
                let mut members = Vec::new();
                if !self.eat(b'}') {
                    loop {
                        self.space();
                        let key = self.string()?;
                        if !self.eat(b':') {
                            return self.fail();
                        }
                        members.push((key, self.value()?));
                        if self.eat(b'}') {
                            break;
                        }
                        if !self.eat(b',') {
                            return self.fail();
                        }
                    }
                }
                Ok(Value::Object(members))
            }
            Some(c) if c.is_ascii_digit() || *c == b'-' => {
                let n = rest.iter().take_while(|c| c.is_ascii_digit() || b"+-.eE".contains(c)).count();
                self.i += n;
                Ok(Value::Number(String::from_utf8_lossy(&rest[..n]).into()))
            }
            _ => self.fail(),
        }
    }
    fn string(&mut self) -> Result<String, String> {
        if self.b.get(self.i) != Some(&b'"') {
            return self.fail();
        }
        self.i += 1;
        let mut out = Vec::new();
        loop {
            let Some(&c) = self.b.get(self.i) else { return self.fail() };
            self.i += 1;
            match c {
                b'"' => return String::from_utf8(out).map_err(|e| e.to_string()),
                b'\\' => {
                    let Some(&e) = self.b.get(self.i) else { return self.fail() };
                    self.i += 1;
                    let ch = match e {
                        b'"' | b'\\' | b'/' => e as char,
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => {
                            let hex = self.b.get(self.i..self.i + 4).and_then(|h| std::str::from_utf8(h).ok());
                            let code = hex.and_then(|h| u32::from_str_radix(h, 16).ok());
                            let Some(code) = code else { return self.fail() };
                            self.i += 4;
                            char::from_u32(code).unwrap_or('\u{fffd}')
                        }
                        _ => return self.fail(),
                    };
                    out.extend(ch.encode_utf8(&mut [0; 4]).as_bytes());
                }
                _ => out.push(c),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_values_escapes_and_errors() {
        let v = parse(r#" {"a": [1, -2.5e3, true, null], "b": {"c": "x\"A\n"}} "#).unwrap();
        assert_eq!(v.get("b").and_then(|b| b.get("c")).and_then(Value::as_str), Some("x\"A\n"));
        assert_eq!(
            v.get("a"),
            Some(&Value::Array(vec![Value::Number("1".into()), Value::Number("-2.5e3".into()), Value::Bool(true), Value::Null]))
        );
        for bad in ["", "{", "[1,]", "{\"a\" 1}", "\"abc", "{} x", "[1 2]"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
    }
}
