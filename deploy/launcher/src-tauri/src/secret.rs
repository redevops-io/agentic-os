//! Credentials. They live in the OS credential store (Keychain / Credential Manager / Secret Service) and nowhere
//! else: never in the connection file, never in a URL, never in a log line, never returned to a web page.
//! There is deliberately NO plaintext fallback — if the platform has no credential store, saving a token is refused.

#[cfg(test)]
use std::collections::HashMap;
#[cfg(test)]
use std::sync::Mutex;
use zeroize::Zeroize;

pub const SERVICE: &str = "io.redevops.agentic-apps";
const MAX_LEN: usize = 4096;

/// A secret string whose Debug/Display never reveal it, and which is wiped from memory when dropped.
pub struct Secret(String);

impl Secret {
    pub fn new(value: impl Into<String>) -> Result<Self, StoreError> {
        let v: String = value.into();
        if v.is_empty() || v.len() > MAX_LEN || v.chars().any(|c| c.is_control() || c.is_whitespace()) {
            return Err(StoreError::Invalid);
        }
        Ok(Secret(v))
    }
    /// The only way to read it — used when building an Authorization header and nowhere else.
    pub fn expose(&self) -> &str {
        &self.0
    }
}
impl Drop for Secret {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}
impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Secret(***)")
    }
}
impl std::fmt::Display for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "***")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    /// not a usable token (empty, too long, whitespace/control characters)
    Invalid,
    /// this machine has no usable credential store — we refuse rather than fall back to a file
    Unavailable,
}
impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Invalid => write!(f, "That doesn't look like a valid access token."),
            StoreError::Unavailable => write!(f, "This computer has no secure credential store available, so the token was not saved."),
        }
    }
}

pub trait SecretStore: Send + Sync {
    fn get(&self, account: &str) -> Result<Option<Secret>, StoreError>;
    fn set(&self, account: &str, secret: &Secret) -> Result<(), StoreError>;
    fn delete(&self, account: &str) -> Result<(), StoreError>;
    /// Whether a credential can be stored at all (drives the UI; never reveals a value).
    fn available(&self) -> bool;
}

/// The real thing: Keychain / Credential Manager / Secret Service.
pub struct KeyringStore;

impl KeyringStore {
    fn entry(account: &str) -> Result<keyring::Entry, StoreError> {
        keyring::Entry::new(SERVICE, account).map_err(|_| StoreError::Unavailable)
    }
}
impl SecretStore for KeyringStore {
    fn get(&self, account: &str) -> Result<Option<Secret>, StoreError> {
        match Self::entry(account)?.get_password() {
            Ok(p) => Secret::new(p).map(Some),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err(StoreError::Unavailable),
        }
    }
    fn set(&self, account: &str, secret: &Secret) -> Result<(), StoreError> {
        Self::entry(account)?.set_password(secret.expose()).map_err(|_| StoreError::Unavailable)
    }
    fn delete(&self, account: &str) -> Result<(), StoreError> {
        match Self::entry(account)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(StoreError::Unavailable),
        }
    }
    fn available(&self) -> bool {
        // a lookup of a key that does not exist succeeds (NoEntry) iff the store itself is reachable
        matches!(Self::entry("probe:availability").map(|e| e.get_password()), Ok(Ok(_)) | Ok(Err(keyring::Error::NoEntry)))
    }
}

/// In-memory store, tests only. Production has NO such fallback: without a credential store, saving a token is refused.
#[cfg(test)]
pub struct MemoryStore {
    inner: Mutex<HashMap<String, String>>,
    usable: bool,
}
#[cfg(test)]
impl MemoryStore {
    pub fn new() -> Self {
        MemoryStore { inner: Mutex::new(HashMap::new()), usable: true }
    }
    pub fn unavailable() -> Self {
        MemoryStore { inner: Mutex::new(HashMap::new()), usable: false }
    }
}
#[cfg(test)]
impl SecretStore for MemoryStore {
    fn get(&self, account: &str) -> Result<Option<Secret>, StoreError> {
        if !self.usable { return Err(StoreError::Unavailable); }
        self.inner.lock().unwrap().get(account).cloned().map(Secret::new).transpose()
    }
    fn set(&self, account: &str, secret: &Secret) -> Result<(), StoreError> {
        if !self.usable { return Err(StoreError::Unavailable); }
        self.inner.lock().unwrap().insert(account.to_string(), secret.expose().to_string());
        Ok(())
    }
    fn delete(&self, account: &str) -> Result<(), StoreError> {
        if !self.usable { return Err(StoreError::Unavailable); }
        self.inner.lock().unwrap().remove(account);
        Ok(())
    }
    fn available(&self) -> bool {
        self.usable
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_secret_never_prints() {
        let s = Secret::new("sk-live-ABCDEF123456").unwrap();
        for text in [format!("{s:?}"), format!("{s}"), format!("{:#?}", s), format!("{:?}", Some(&s)), format!("{:?}", vec![&s])] {
            assert!(!text.contains("ABCDEF"), "leaked: {text}");
            assert!(text.contains("***"));
        }
        assert_eq!(s.expose(), "sk-live-ABCDEF123456");
    }

    #[test]
    fn rejects_unusable_tokens() {
        for bad in ["", " ", "a b", "tab\there", "new\nline", &"x".repeat(4097)] {
            assert_eq!(Secret::new(bad).err(), Some(StoreError::Invalid), "{bad:?}");
        }
        assert!(Secret::new("a".repeat(4096)).is_ok());
    }

    #[test]
    fn store_roundtrips_and_deletes() {
        let st = MemoryStore::new();
        assert!(st.get("conn:a").unwrap().is_none());
        st.set("conn:a", &Secret::new("tok1").unwrap()).unwrap();
        assert_eq!(st.get("conn:a").unwrap().unwrap().expose(), "tok1");
        assert!(st.get("conn:b").unwrap().is_none());               // scoped per connection
        st.delete("conn:a").unwrap();
        assert!(st.get("conn:a").unwrap().is_none());
        st.delete("conn:a").unwrap();                                // deleting nothing is fine
    }

    #[test]
    fn without_a_credential_store_it_refuses_rather_than_falling_back() {
        let st = MemoryStore::unavailable();
        assert!(!st.available());
        assert_eq!(st.set("conn:a", &Secret::new("tok").unwrap()), Err(StoreError::Unavailable));
        assert!(matches!(st.get("conn:a"), Err(StoreError::Unavailable)));
    }
}
