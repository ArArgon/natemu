//! Shapes the JSON reports share (the run report, the performance log, test-driver output); serialized with serde_json.
use serde::{Serialize, Serializer};

/// A number written as a quoted hexadecimal string (`"0x10"`), as the reports give addresses and register values.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hex(pub u64);
impl Serialize for Hex {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.collect_str(&format_args!("{:#x}", self.0))
    }
}
/// Each of `values` as [`Hex`].
pub fn hexes(values: &[u64]) -> Vec<Hex> {
    values.iter().copied().map(Hex).collect()
}
/// A JSON object from `(key, value)` pairs, written in their order (serde_json's own map would sort the keys).
#[derive(Debug, Default)]
pub struct Members<K, V>(pub Vec<(K, V)>);
impl<K: Serialize, V: Serialize> Serialize for Members<K, V> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.collect_map(self.0.iter().map(|(k, v)| (k, v)))
    }
}
impl<K, V> FromIterator<(K, V)> for Members<K, V> {
    fn from_iter<I: IntoIterator<Item = (K, V)>>(pairs: I) -> Self {
        Self(pairs.into_iter().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hex_strings_and_ordered_members() {
        let m: Members<&str, Vec<Hex>> = [("b", hexes(&[0, 16])), ("a", vec![])]
            .into_iter()
            .collect();
        assert_eq!(
            serde_json::to_string(&m).unwrap(),
            r#"{"b":["0x0","0x10"],"a":[]}"#
        );
    }
}
