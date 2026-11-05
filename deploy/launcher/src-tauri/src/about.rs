// About / Legal data for the launcher window (RC3, U-03). Two read-only commands that serve FIXED files of
// the application's own `Licenses` resource folder to the launcher page. Properties, each covered by a test:
//   * the page never supplies a path: `about_text` takes a name that must equal one of six constants, and the
//     file opened is built from the constant, never from the input;
//   * the record `about.json` and each text are opened ONCE without following a link (the Licenses folder and the
//     file can not be symlinks), typed and size-checked on the OPEN handle, and read from that same handle under a
//     hard byte cap; there is no second path-based access (no lstat-then-open gap);
//   * a text is only served when the build record lists it with a sha256 and the bytes match it;
//   * a missing or malformed record gives a clear, coded message; nothing is created, written or fetched;
//   * no network, no sidecar: it works with the control plane stopped.
// This module shows what the build record contains. It makes no statement about licences or legal duties.

use sha2::{Digest, Sha256};
use std::fmt;
use std::io::Read;
use std::path::Path;

/// Folder name of the staged Licenses resource (`bundle.resources`: `"licenses/": "Licenses/"`).
pub const LICENSES_DIR: &str = "Licenses";
/// The build record (schema `redevops.about/1`, produced by packaging/notices/generate_about.py).
pub const RECORD_FILE: &str = "about.json";
pub const RECORD_SCHEMA: &str = "redevops.about/1";
/// The record is at most 1 MiB by construction (generator); the launcher refuses more than 2 MiB.
pub const MAX_RECORD_BYTES: u64 = 2 * 1024 * 1024;
/// THIRD_PARTY_NOTICES.txt is about 1.2 MB in RC2; refuse more than 4 MiB.
pub const MAX_TEXT_BYTES: u64 = 4 * 1024 * 1024;

/// The ONLY files `about_text` can ever open. The page-supplied name is compared with these constants.
pub const TEXT_FILES: [&str; 6] = [
    "LICENSE",
    "LICENSE-COMMONS-CLAUSE",
    "LICENSE.md",
    "LICENSING.md",
    "THIRD_PARTY_NOTICES.txt",
    "NOTICES_MISSING.md",
];

#[derive(Debug, PartialEq, Eq)]
pub enum AboutError {
    /// `about.json` (or the Licenses folder) is not in this build.
    RecordMissing,
    /// Present but not valid JSON, wrong schema, or not an object.
    RecordInvalid(&'static str),
    /// The requested name is not one of the six fixed files.
    UnknownFile,
    /// The file is not in this build (or not a regular file).
    FileMissing(&'static str),
    /// Over the size limit.
    TooLarge(&'static str),
    /// The record does not list the file, or lists no usable sha256.
    NotInRecord,
    /// The bytes do not match the record's sha256.
    HashMismatch,
    /// Bytes are not UTF-8 text.
    NotText,
    /// Any other I/O failure (kind only, never a path).
    Io(std::io::ErrorKind),
}

impl AboutError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::RecordMissing => "ABOUT_RECORD_MISSING",
            Self::RecordInvalid(_) => "ABOUT_RECORD_INVALID",
            Self::UnknownFile => "ABOUT_UNKNOWN_FILE",
            Self::FileMissing(_) => "ABOUT_FILE_MISSING",
            Self::TooLarge(_) => "ABOUT_TOO_LARGE",
            Self::NotInRecord => "ABOUT_NOT_IN_RECORD",
            Self::HashMismatch => "ABOUT_HASH_MISMATCH",
            Self::NotText => "ABOUT_NOT_TEXT",
            Self::Io(_) => "ABOUT_IO",
        }
    }
}

impl fmt::Display for AboutError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let msg: String = match self {
            Self::RecordMissing => "The build record (Licenses/about.json) is not part of this build. Nothing can be shown here; the Licenses folder of the application may still hold the licence files.".into(),
            Self::RecordInvalid(why) => format!("The build record (Licenses/about.json) cannot be used: {why}."),
            Self::UnknownFile => "That file is not one of the files this view can show.".into(),
            Self::FileMissing(what) => format!("The file is not available in this build ({what})."),
            Self::TooLarge(what) => format!("The file is larger than this view reads ({what})."),
            Self::NotInRecord => "The build record does not list that file with a checksum, so it is not shown.".into(),
            Self::HashMismatch => "The file does not match the checksum in the build record, so it is not shown.".into(),
            Self::NotText => "The file is not UTF-8 text, so it is not shown.".into(),
            Self::Io(kind) => format!("The file could not be read ({kind})."),
        };
        write!(f, "{}: {}", self.code(), msg)
    }
}

/// Test seam: closures run at the two moments a path-based implementation would be racy. Production code passes none.
#[derive(Default)]
struct Hooks<'a> {
    /// Runs right before the file is opened (a swap here must not be followed).
    before_open: Option<&'a dyn Fn()>,
    /// Runs right after the file is open (a swap here must not change what is read: the handle is already bound).
    after_open: Option<&'a dyn Fn()>,
}

/// Open `dir/name` ONCE, without following a link, and return the open handle.
/// Unix: the folder is opened with O_NOFOLLOW|O_DIRECTORY and the file with `openat` on that folder handle with
/// O_NOFOLLOW (|O_NONBLOCK so a FIFO cannot block the call), so neither the `Licenses` folder nor the file can be a
/// symlink and there is no second path lookup. Windows: the file is opened with FILE_FLAG_OPEN_REPARSE_POINT and
/// the handle's attributes are checked for a reparse point; the folder is checked for a reparse point first
/// (a path-based check, residual: a folder swapped for a junction between that check and the open).
#[cfg(unix)]
fn open_no_follow(dir: &Path, name: &str) -> std::io::Result<std::fs::File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;
    let cdir = CString::new(dir.as_os_str().as_bytes()).map_err(|_| std::io::ErrorKind::InvalidInput)?;
    let cname = CString::new(name).map_err(|_| std::io::ErrorKind::InvalidInput)?;
    // SAFETY: valid NUL-terminated strings; the returned descriptors are owned by OwnedFd/File immediately.
    let dfd = unsafe { libc::open(cdir.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if dfd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let dirfd = unsafe { OwnedFd::from_raw_fd(dfd) };
    let fd = unsafe { libc::openat(dirfd.as_raw_fd(), cname.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { std::fs::File::from_raw_fd(fd) })
}

#[cfg(windows)]
fn open_no_follow(dir: &Path, name: &str) -> std::io::Result<std::fs::File> {
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    let dmeta = std::fs::symlink_metadata(dir)?;
    if dmeta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 || !dmeta.is_dir() {
        return Err(std::io::ErrorKind::NotFound.into());
    }
    let file = std::fs::OpenOptions::new().read(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(dir.join(name))?;
    if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(std::io::ErrorKind::NotFound.into());
    }
    Ok(file)
}

/// Is this open error "there is no usable regular file here" (absent, or a link/folder/odd node refused by the flags)?
fn is_absent(e: &std::io::Error) -> bool {
    if e.kind() == std::io::ErrorKind::NotFound {
        return true;
    }
    #[cfg(unix)]
    if let Some(c) = e.raw_os_error() {
        // ELOOP: O_NOFOLLOW met a symlink; ENOTDIR: the folder is not a folder; ENXIO/EISDIR: device-less FIFO / directory.
        return [libc::ELOOP, libc::ENOTDIR, libc::ENXIO, libc::EISDIR].contains(&c);
    }
    false
}

/// Read a regular file `dir/name` (name is a constant of this module) into a string, bounded by `cap`.
/// One open, no link followed; type and size are taken from the OPEN handle (fstat); the bytes are read from that
/// same handle with a hard cap. No second path-based access happens after the open.
fn read_capped(dir: &Path, name: &str, cap: u64, missing: AboutError, large: &'static str) -> Result<String, AboutError> {
    read_capped_hooked(dir, name, cap, missing, large, &Hooks::default())
}

fn read_capped_hooked(dir: &Path, name: &str, cap: u64, missing: AboutError, large: &'static str, hooks: &Hooks) -> Result<String, AboutError> {
    if let Some(h) = hooks.before_open {
        h();
    }
    let file = match open_no_follow(dir, name) {
        Ok(f) => f,
        Err(e) if is_absent(&e) => return Err(missing),
        Err(e) => return Err(AboutError::Io(e.kind())),
    };
    if let Some(h) = hooks.after_open {
        h();
    }
    let meta = file.metadata().map_err(|e| AboutError::Io(e.kind()))?;
    if !meta.is_file() {
        return Err(missing); // FIFO, device, directory, socket
    }
    if meta.len() > cap {
        return Err(AboutError::TooLarge(large));
    }
    // Bounded again at read time (the file may grow after the fstat); the same handle is read.
    let mut buf = Vec::new();
    file.take(cap + 1).read_to_end(&mut buf).map_err(|e| AboutError::Io(e.kind()))?;
    if buf.len() as u64 > cap {
        return Err(AboutError::TooLarge(large));
    }
    String::from_utf8(buf).map_err(|_| AboutError::NotText)
}

/// Slot states of `redevops.about/1` (agentic_os/about_record.py) with their fixed tags. The old `placeholder` /
/// `filled` states no longer exist and are refused. A contract test compares these constants with about_record.py.
pub const SLOT_TAGS: [(&str, &str); 4] = [
    ("pending", "Pending a decision"),
    ("approved-verified", "Approved text, signature verified"),
    ("unverified-draft", "Unverified draft, not an approved text"),
    ("not-applicable", ""),
];
/// The label an unverified draft carries INSIDE its text (so no renderer can show it as ordinary legal text).
pub const DRAFT_PREFIX: &str = "Unverified draft, not an approved text. This is a private candidate; nothing here is a legal statement or clearance.\n\n";
pub const PLACEHOLDER_PREFIX: &str = "Legal text pending decision ";
pub const SOURCE_ACCESS_STATES: [&str; 2] = ["not-published", "offer-recorded"];

/// Check the legal slots and `source_access` the way the view relies on them (a subset of validate_record).
fn check_legal(v: &serde_json::Value) -> Result<(), AboutError> {
    const BAD: AboutError = AboutError::RecordInvalid("its legal slots do not follow redevops.about/1");
    let slots = v.get("legal").and_then(|l| l.get("slots")).and_then(|s| s.as_object()).ok_or(BAD)?;
    for slot in slots.values() {
        let state = slot.get("state").and_then(|s| s.as_str()).ok_or(BAD)?;
        let tag = slot.get("tag").and_then(|s| s.as_str()).ok_or(BAD)?;
        let text = slot.get("text").and_then(|s| s.as_str()).ok_or(BAD)?;
        let want = SLOT_TAGS.iter().find(|(s, _)| *s == state).map(|(_, t)| *t).ok_or(BAD)?;
        if tag != want {
            return Err(BAD);
        }
        let sha = slot.get("text_sha256").and_then(|s| s.as_str());
        let sha_of = |s: &str| hex(&Sha256::digest(s.as_bytes()));
        let ok = match state {
            "pending" => text.starts_with(PLACEHOLDER_PREFIX),
            "approved-verified" => !text.trim().is_empty() && sha == Some(sha_of(text).as_str()),
            "unverified-draft" => text.starts_with(DRAFT_PREFIX) && sha == Some(sha_of(&text[DRAFT_PREFIX.len()..]).as_str()),
            _ => text.is_empty(), // not-applicable
        };
        if !ok {
            return Err(BAD);
        }
    }
    let state = v.get("source_access").and_then(|s| s.get("state")).and_then(|s| s.as_str());
    if !state.is_some_and(|s| SOURCE_ACCESS_STATES.contains(&s)) {
        return Err(AboutError::RecordInvalid("its source_access state is not one the view knows"));
    }
    Ok(())
}

/// Parse and minimally validate the record text. The page validates the shape in more detail; this is the
/// gate that keeps a malformed, foreign or old-schema file from being handed over as "the build record".
fn parse_record(text: &str) -> Result<serde_json::Value, AboutError> {
    let v: serde_json::Value =
        serde_json::from_str(text).map_err(|_| AboutError::RecordInvalid("it is not valid JSON"))?;
    if !v.is_object() {
        return Err(AboutError::RecordInvalid("it is not a JSON object"));
    }
    if v.get("schema").and_then(|s| s.as_str()) != Some(RECORD_SCHEMA) {
        return Err(AboutError::RecordInvalid("its schema is not redevops.about/1"));
    }
    check_legal(&v)?;
    Ok(v)
}

/// `about_info`: the build record, as validated JSON text.
pub fn load_record(dir: &Path) -> Result<String, AboutError> {
    let text = read_capped(dir, RECORD_FILE, MAX_RECORD_BYTES, AboutError::RecordMissing, "2 MiB limit for the build record")?;
    parse_record(&text)?;
    Ok(text)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The sha256 the record lists for `file` (in `licence_files[]` or `notices`), lowercase; None if absent/unusable.
fn recorded_sha256(record: &serde_json::Value, file: &str) -> Option<String> {
    let valid = |s: &str| s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit());
    let from_entry = |e: &serde_json::Value| -> Option<String> {
        if e.get("file").and_then(|f| f.as_str()) != Some(file) {
            return None;
        }
        let s = e.get("sha256").and_then(|s| s.as_str())?;
        valid(s).then(|| s.to_ascii_lowercase())
    };
    if let Some(list) = record.get("licence_files").and_then(|l| l.as_array()) {
        if let Some(h) = list.iter().find_map(from_entry) {
            return Some(h);
        }
    }
    record.get("notices").and_then(from_entry)
}

/// `about_text`: one of the six fixed files. `name` is only ever COMPARED; the opened file is the constant.
pub fn load_text(dir: &Path, name: &str) -> Result<String, AboutError> {
    let file: &'static str = TEXT_FILES.iter().copied().find(|f| *f == name).ok_or(AboutError::UnknownFile)?;
    let record = parse_record(&load_record(dir)?)?;
    let want = recorded_sha256(&record, file).ok_or(AboutError::NotInRecord)?;
    let text = read_capped(dir, file, MAX_TEXT_BYTES, AboutError::FileMissing("it is not in the Licenses folder"), "4 MiB limit")?;
    if hex(&Sha256::digest(text.as_bytes())) != want {
        return Err(AboutError::HashMismatch);
    }
    Ok(text)
}

fn licenses_dir(app: &tauri::AppHandle) -> Result<std::path::PathBuf, AboutError> {
    use tauri::Manager;
    app.path().resource_dir().map(|d| d.join(LICENSES_DIR)).map_err(|_| AboutError::RecordMissing)
}

/// The build record (`Licenses/about.json`). Read-only; no argument from the page.
#[tauri::command]
pub async fn about_info(app: tauri::AppHandle) -> Result<String, String> {
    let dir = licenses_dir(&app).map_err(|e| e.to_string())?;
    load_record(&dir).map_err(|e| e.to_string())
}

/// One fixed text file of the Licenses folder. `slug` must be one of [`TEXT_FILES`] (exact match).
#[tauri::command]
pub async fn about_text(app: tauri::AppHandle, slug: String) -> Result<String, String> {
    let dir = licenses_dir(&app).map_err(|e| e.to_string())?;
    load_text(&dir, &slug).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static N: AtomicUsize = AtomicUsize::new(0);

    fn scratch(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("rdo-about-test-{tag}-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn sha(text: &str) -> String {
        hex(&Sha256::digest(text.as_bytes()))
    }

    /// A `legal` + `source_access` block in the redevops.about/1 shape (all slots pending).
    fn legal_block() -> serde_json::Value {
        let ph = |ids: &[&str]| serde_json::json!({"state": "pending", "tag": "Pending a decision", "decisions": ids, "pending": ids,
            "text": format!("Legal text pending decision {}. This is a private candidate; nothing here is a legal statement or clearance.", ids.join(", "))});
        serde_json::json!({"slots": {
            "LEGAL.COPYRIGHT_LINE": ph(&["D-LIC-1"]), "LEGAL.NO_WARRANTY": ph(&["D-LIC-10"]), "LEGAL.SECTION13_NOTICE": ph(&["D-LIC-5"])}})
    }

    /// A folder with the six files and a record that lists them (fixture copy of the redevops.about/1 shape).
    fn build(tag: &str) -> std::path::PathBuf {
        let d = scratch(tag);
        let mut files = Vec::new();
        for f in TEXT_FILES {
            let body = format!("text of {f}\nsecond line\n");
            std::fs::write(d.join(f), &body).unwrap();
            if f != "THIRD_PARTY_NOTICES.txt" {
                files.push(serde_json::json!({"id": f.to_ascii_lowercase(), "file": f, "bytes": body.len(), "sha256": sha(&body)}));
            }
        }
        let notices = "text of THIRD_PARTY_NOTICES.txt\nsecond line\n".to_string();
        let rec = serde_json::json!({
            "schema": RECORD_SCHEMA,
            "product": {"name": "Agentic Apps R0-prime"},
            "licence_files": files,
            "notices": {"file": "THIRD_PARTY_NOTICES.txt", "bytes": notices.len(), "sha256": sha(&notices), "components": []},
            "legal": legal_block(),
            "source_access": {"state": "not-published"},
        });
        std::fs::write(d.join(RECORD_FILE), serde_json::to_string(&rec).unwrap()).unwrap();
        d
    }

    #[test]
    fn the_fixed_name_list_is_exactly_the_six_shipped_text_files() {
        assert_eq!(
            TEXT_FILES,
            ["LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "THIRD_PARTY_NOTICES.txt", "NOTICES_MISSING.md"]
        );
        assert!(TEXT_FILES.iter().all(|f| !f.contains('/') && !f.contains('\\') && !f.contains("..")));
    }

    #[test]
    fn the_record_is_returned_as_validated_json_text() {
        let d = build("rec");
        let text = load_record(&d).unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["schema"], RECORD_SCHEMA);
    }

    #[test]
    fn a_missing_record_or_folder_gives_a_clear_coded_message() {
        let d = scratch("missing");
        let e = load_record(&d).unwrap_err();
        assert_eq!(e, AboutError::RecordMissing);
        assert!(e.to_string().starts_with("ABOUT_RECORD_MISSING: The build record (Licenses/about.json) is not part of this build"), "{e}");
        let gone = d.join("no-such-folder");
        assert_eq!(load_record(&gone).unwrap_err(), AboutError::RecordMissing);
        assert_eq!(load_text(&gone, "LICENSE").unwrap_err(), AboutError::RecordMissing);
    }

    #[test]
    fn malformed_json_wrong_schema_and_non_objects_are_refused_with_a_reason() {
        for (body, why) in [
            ("{not json", "not valid JSON"),
            ("", "not valid JSON"),
            ("[]", "not a JSON object"),
            ("\"redevops.about/1\"", "not a JSON object"),
            ("{}", "schema"),
            ("{\"schema\": \"redevops.about/1\"}", "legal slots"),
            ("{\"schema\": \"redevops.about/2\"}", "schema"),
            ("{\"schema\": 1}", "schema"),
        ] {
            let d = scratch("bad");
            std::fs::write(d.join(RECORD_FILE), body).unwrap();
            let e = load_record(&d).unwrap_err();
            assert_eq!(e.code(), "ABOUT_RECORD_INVALID", "{body}");
            assert!(e.to_string().contains(why), "{body}: {e}");
        }
        let d = scratch("nonutf8");
        std::fs::write(d.join(RECORD_FILE), [0xffu8, 0xfe, 0x00]).unwrap();
        assert_eq!(load_record(&d).unwrap_err(), AboutError::NotText);
    }

    #[test]
    fn an_oversize_record_is_refused_before_it_is_read_whole() {
        let d = scratch("bigrec");
        let f = std::fs::File::create(d.join(RECORD_FILE)).unwrap();
        f.set_len(MAX_RECORD_BYTES + 1).unwrap(); // sparse: cheap
        assert_eq!(load_record(&d).unwrap_err().code(), "ABOUT_TOO_LARGE");
    }

    #[test]
    fn every_fixed_text_is_served_when_the_record_lists_it_with_a_matching_checksum() {
        let d = build("texts");
        for f in TEXT_FILES {
            assert_eq!(load_text(&d, f).unwrap(), format!("text of {f}\nsecond line\n"), "{f}");
        }
    }

    #[test]
    fn names_outside_the_fixed_list_never_open_a_file_no_traversal_no_paths_no_case_games() {
        let d = build("trav");
        // a secret next to the folder that a traversal would reach
        let outside = d.parent().unwrap().join(format!("rdo-about-outside-{}", std::process::id()));
        std::fs::write(&outside, "secret").unwrap();
        let outside_name = outside.file_name().unwrap().to_string_lossy().into_owned();
        for bad in [
            "", " ", "license", "Licence", "LICENSE ", " LICENSE", "LICENSE\0", "LICENSE/", "./LICENSE", "LICENSE/../LICENSE",
            "../about.json", "..", ".", "about.json", "LICENSE.md.bak", "licenses/LICENSE", "Licenses/LICENSE",
            "/etc/passwd", "C:\\Windows\\win.ini", "..\\LICENSE", "%2e%2e/LICENSE", "THIRD_PARTY_NOTICES.TXT",
            &format!("../{outside_name}"), outside.to_str().unwrap(),
        ] {
            assert_eq!(load_text(&d, bad).unwrap_err(), AboutError::UnknownFile, "{bad:?}");
        }
        let _ = std::fs::remove_file(&outside);
    }

    #[test]
    fn the_record_is_the_authority_a_file_it_does_not_list_is_not_shown() {
        let d = build("unlisted");
        let mut rec: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(d.join(RECORD_FILE)).unwrap()).unwrap();
        rec["licence_files"] = serde_json::json!([]);
        std::fs::write(d.join(RECORD_FILE), rec.to_string()).unwrap();
        assert_eq!(load_text(&d, "LICENSE").unwrap_err(), AboutError::NotInRecord);
        // a listed file with an unusable checksum is the same as not listed
        rec["licence_files"] = serde_json::json!([{"file": "LICENSE", "sha256": "xyz"}, {"file": "LICENSE.md"}]);
        std::fs::write(d.join(RECORD_FILE), rec.to_string()).unwrap();
        assert_eq!(load_text(&d, "LICENSE").unwrap_err(), AboutError::NotInRecord);
        assert_eq!(load_text(&d, "LICENSE.md").unwrap_err(), AboutError::NotInRecord);
    }

    #[test]
    fn a_file_that_differs_from_the_recorded_checksum_is_not_shown() {
        let d = build("tamper");
        std::fs::write(d.join("LICENSE"), "changed after the record was made\n").unwrap();
        let e = load_text(&d, "LICENSE").unwrap_err();
        assert_eq!(e, AboutError::HashMismatch);
        assert!(e.to_string().contains("does not match the checksum"));
        assert!(load_text(&d, "LICENSE.md").is_ok()); // others unaffected
    }

    #[test]
    fn a_listed_but_absent_file_says_so() {
        let d = build("absent");
        std::fs::remove_file(d.join("NOTICES_MISSING.md")).unwrap();
        let e = load_text(&d, "NOTICES_MISSING.md").unwrap_err();
        assert_eq!(e.code(), "ABOUT_FILE_MISSING");
        assert!(e.to_string().contains("not available in this build"));
    }

    #[test]
    fn an_oversize_text_is_refused_by_size_before_any_checksum() {
        let d = build("bigtext");
        let f = std::fs::OpenOptions::new().write(true).open(d.join("THIRD_PARTY_NOTICES.txt")).unwrap();
        f.set_len(MAX_TEXT_BYTES + 1).unwrap();
        assert_eq!(load_text(&d, "THIRD_PARTY_NOTICES.txt").unwrap_err().code(), "ABOUT_TOO_LARGE");
    }

    #[test]
    fn a_text_exactly_at_the_limit_is_still_read_but_one_byte_more_is_not() {
        // the limit check is on the byte count; the checksum then decides, so build the record around the content
        let d = scratch("limit");
        let body = "a".repeat(MAX_TEXT_BYTES as usize);
        std::fs::write(d.join("LICENSE"), &body).unwrap();
        let rec = serde_json::json!({"schema": RECORD_SCHEMA, "legal": legal_block(), "source_access": {"state": "not-published"}, "licence_files": [{"file": "LICENSE", "sha256": sha(&body)}]});
        std::fs::write(d.join(RECORD_FILE), rec.to_string()).unwrap();
        assert_eq!(load_text(&d, "LICENSE").unwrap().len() as u64, MAX_TEXT_BYTES);
        std::fs::write(d.join("LICENSE"), format!("{body}a")).unwrap();
        assert_eq!(load_text(&d, "LICENSE").unwrap_err().code(), "ABOUT_TOO_LARGE");
    }

    #[test]
    fn non_utf8_text_is_refused_not_lossily_shown() {
        let d = build("bin");
        let bytes = [b'a', 0xff, b'b'];
        std::fs::write(d.join("LICENSE"), bytes).unwrap();
        let mut rec: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(d.join(RECORD_FILE)).unwrap()).unwrap();
        rec["licence_files"] = serde_json::json!([{"file": "LICENSE", "sha256": hex(&Sha256::digest(bytes))}]);
        std::fs::write(d.join(RECORD_FILE), rec.to_string()).unwrap();
        assert_eq!(load_text(&d, "LICENSE").unwrap_err(), AboutError::NotText);
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_or_directory_in_place_of_a_file_is_refused() {
        let d = build("link");
        let outside = d.parent().unwrap().join(format!("rdo-about-link-target-{}", std::process::id()));
        std::fs::write(&outside, "text of LICENSE\nsecond line\n").unwrap(); // same bytes: only the link is wrong
        std::fs::remove_file(d.join("LICENSE")).unwrap();
        std::os::unix::fs::symlink(&outside, d.join("LICENSE")).unwrap();
        assert_eq!(load_text(&d, "LICENSE").unwrap_err().code(), "ABOUT_FILE_MISSING");
        // the record itself as a symlink
        std::fs::rename(d.join(RECORD_FILE), d.join("real-record")).unwrap();
        std::os::unix::fs::symlink(d.join("real-record"), d.join(RECORD_FILE)).unwrap();
        assert_eq!(load_record(&d).unwrap_err(), AboutError::RecordMissing);
        // a directory named like a file
        std::fs::remove_file(d.join("LICENSE.md")).unwrap();
        std::fs::create_dir(d.join("LICENSE.md")).unwrap();
        assert!(matches!(load_text(&d, "LICENSE.md").unwrap_err(), AboutError::RecordMissing | AboutError::FileMissing(_)));
        let _ = std::fs::remove_file(&outside);
    }

    #[test]
    fn messages_carry_a_code_and_never_a_path() {
        let d = scratch("nopath");
        let all = [
            load_record(&d).unwrap_err(),
            load_text(&d, "nope").unwrap_err(),
            AboutError::FileMissing("x"),
            AboutError::Io(std::io::ErrorKind::PermissionDenied),
            AboutError::HashMismatch,
        ];
        for e in all {
            let m = e.to_string();
            assert!(m.starts_with(e.code()) && m.contains(": "), "{m}");
            assert!(!m.contains(d.to_str().unwrap()) && !m.contains("/private/") && !m.contains("/tmp"), "{m}");
        }
    }

    #[test]
    fn the_commands_take_no_path_and_the_module_has_no_network_process_or_write_calls() {
        let src = include_str!("about.rs");
        let code = src.split("#[cfg(test)]").next().unwrap();
        for banned in ["TcpStream", "reqwest", "Command::new", "std::fs::write", "File::create", "remove_file", "create_dir", "std::env::var", "http://", "https://"] {
            assert!(!code.contains(banned), "about.rs must not use {banned}");
        }
        assert!(!code.contains("File::open(") && !code.contains("fs::read("), "files are opened only through open_no_follow");
        assert!(code.matches("fs::symlink_metadata").count() == 1, "the only path-based metadata call is the Windows folder check");
        assert!(code.contains("pub async fn about_info(app: tauri::AppHandle)"));
        assert!(code.contains("pub async fn about_text(app: tauri::AppHandle, slug: String)"));
    }

    // ---- RC3 review fix: open once without following links, fstat the open handle, read that handle ----

    #[cfg(unix)]
    fn read_hooked(d: &Path, name: &str, cap: u64, hooks: &Hooks) -> Result<String, AboutError> {
        read_capped_hooked(d, name, cap, AboutError::FileMissing("x"), "cap", hooks)
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_to_a_regular_file_is_never_opened_for_record_or_text() {
        let d = build("nofollow-file");
        let target = d.join("real-copy");
        std::fs::write(&target, "text of LICENSE\nsecond line\n").unwrap();
        std::fs::remove_file(d.join("LICENSE")).unwrap();
        std::os::unix::fs::symlink(&target, d.join("LICENSE")).unwrap();
        assert_eq!(read_hooked(&d, "LICENSE", 1024, &Hooks::default()).unwrap_err(), AboutError::FileMissing("x"));
        assert_eq!(load_text(&d, "LICENSE").unwrap_err().code(), "ABOUT_FILE_MISSING");
        // a dangling link and a link to a directory behave the same
        std::os::unix::fs::symlink(d.join("nowhere"), d.join("dangling")).unwrap();
        assert_eq!(read_hooked(&d, "dangling", 1024, &Hooks::default()).unwrap_err(), AboutError::FileMissing("x"));
        std::os::unix::fs::symlink(&d, d.join("dirlink")).unwrap();
        assert_eq!(read_hooked(&d, "dirlink", 1024, &Hooks::default()).unwrap_err(), AboutError::FileMissing("x"));
    }

    #[cfg(unix)]
    #[test]
    fn the_licenses_folder_itself_must_not_be_a_symlink() {
        let real = build("nofollow-dir");
        let link = real.parent().unwrap().join(format!("rdo-about-dirlink-{}", std::process::id()));
        let _ = std::fs::remove_file(&link);
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert_eq!(load_record(&link).unwrap_err(), AboutError::RecordMissing);
        assert_eq!(load_text(&link, "LICENSE").unwrap_err(), AboutError::RecordMissing);
        assert!(load_record(&real).is_ok(), "the real folder still works");
        // a regular FILE where the folder should be
        let file_as_dir = real.join("LICENSE");
        assert_eq!(load_record(&file_as_dir).unwrap_err(), AboutError::RecordMissing);
        let _ = std::fs::remove_file(&link);
    }

    #[cfg(unix)]
    #[test]
    fn a_swap_for_a_symlink_just_before_the_open_is_not_followed() {
        // what an lstat-then-open implementation would have accepted: the check passes on a regular file, then the
        // path is swapped for a link to a secret. Here the swap runs right before the single open.
        let d = build("swap-before");
        let secret = d.parent().unwrap().join(format!("rdo-about-secret-{}", std::process::id()));
        std::fs::write(&secret, "SECRET").unwrap();
        let swap = || {
            std::fs::remove_file(d.join("LICENSE")).unwrap();
            std::os::unix::fs::symlink(&secret, d.join("LICENSE")).unwrap();
        };
        let r = read_hooked(&d, "LICENSE", 1024, &Hooks { before_open: Some(&swap), after_open: None });
        assert_eq!(r.unwrap_err(), AboutError::FileMissing("x"));
        let _ = std::fs::remove_file(&secret);
    }

    #[cfg(unix)]
    #[test]
    fn a_swap_after_the_open_does_not_change_what_is_read_the_handle_is_bound() {
        let d = build("swap-after");
        let secret = d.parent().unwrap().join(format!("rdo-about-secret2-{}", std::process::id()));
        std::fs::write(&secret, "SECRET").unwrap();
        let swap = || {
            std::fs::remove_file(d.join("LICENSE")).unwrap();
            std::os::unix::fs::symlink(&secret, d.join("LICENSE")).unwrap();
        };
        let got = read_hooked(&d, "LICENSE", 1024, &Hooks { before_open: None, after_open: Some(&swap) }).unwrap();
        assert_eq!(got, "text of LICENSE\nsecond line\n", "the bytes come from the handle opened first, not from the path");
        let _ = std::fs::remove_file(&secret);
    }

    #[cfg(unix)]
    #[test]
    fn a_swap_of_the_licenses_folder_after_the_open_does_not_redirect_the_read() {
        let d = build("swap-folder");
        let other = scratch("swap-folder-other");
        std::fs::write(other.join("LICENSE"), "EVIL").unwrap();
        let moved = d.parent().unwrap().join(format!("rdo-about-moved-{}", std::process::id()));
        let swap = || {
            std::fs::rename(&d, &moved).unwrap();
            std::os::unix::fs::symlink(&other, &d).unwrap();
        };
        let got = read_hooked(&d, "LICENSE", 1024, &Hooks { before_open: None, after_open: Some(&swap) }).unwrap();
        assert_eq!(got, "text of LICENSE\nsecond line\n");
        let _ = std::fs::remove_file(&d);
        let _ = std::fs::rename(&moved, &d);
    }

    #[cfg(unix)]
    #[test]
    fn a_fifo_is_refused_without_blocking() {
        let d = scratch("fifo");
        let p = std::ffi::CString::new(d.join("LICENSE").to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(p.as_ptr(), 0o600) }, 0);
        // no writer exists: a blocking open would hang this test forever
        assert_eq!(read_hooked(&d, "LICENSE", 1024, &Hooks::default()).unwrap_err(), AboutError::FileMissing("x"));
        let rec = d.join(RECORD_FILE);
        let r = std::ffi::CString::new(rec.to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(r.as_ptr(), 0o600) }, 0);
        assert_eq!(load_record(&d).unwrap_err(), AboutError::RecordMissing);
    }

    #[cfg(unix)]
    #[test]
    fn the_size_cap_holds_on_the_handle_even_if_the_file_grows_after_the_size_check() {
        let d = scratch("grow");
        std::fs::write(d.join("LICENSE"), "small").unwrap();
        let grow = || {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new().append(true).open(d.join("LICENSE")).unwrap();
            f.write_all(&vec![b'a'; 4096]).unwrap();
        };
        let r = read_hooked(&d, "LICENSE", 100, &Hooks { before_open: None, after_open: Some(&grow) });
        assert_eq!(r.unwrap_err(), AboutError::TooLarge("cap"));
        // and a file already over the cap is refused from the handle's own size
        std::fs::write(d.join("BIG"), vec![b'a'; 101]).unwrap();
        assert_eq!(read_hooked(&d, "BIG", 100, &Hooks::default()).unwrap_err(), AboutError::TooLarge("cap"));
        std::fs::write(d.join("EXACT"), vec![b'a'; 100]).unwrap();
        assert_eq!(read_hooked(&d, "EXACT", 100, &Hooks::default()).unwrap().len(), 100);
    }

    #[cfg(unix)]
    #[test]
    fn the_checksum_is_over_the_bytes_that_were_read_a_post_open_swap_cannot_make_a_mismatch_pass() {
        // load_text hashes the returned bytes (read from the single handle); a different file at the path afterwards is irrelevant
        let d = build("hash-bound");
        let text = load_text(&d, "LICENSE").unwrap();
        assert_eq!(sha(&text), recorded_sha256(&parse_record(&load_record(&d).unwrap()).unwrap(), "LICENSE").unwrap());
    }

    #[cfg(unix)]
    #[test]
    fn open_errors_stay_path_free() {
        let d = scratch("pathfree");
        std::os::unix::fs::symlink(d.join("x"), d.join("LICENSE")).unwrap();
        let msgs = [
            read_hooked(&d, "LICENSE", 10, &Hooks::default()).unwrap_err().to_string(),
            load_record(&d.join("none")).unwrap_err().to_string(),
            AboutError::Io(std::io::ErrorKind::PermissionDenied).to_string(),
        ];
        for m in msgs {
            assert!(!m.contains(d.to_str().unwrap()) && !m.contains("/private/") && !m.contains("/tmp"), "{m}");
        }
    }

    // ---- round 2: the new slot states; the old ones are refused ----

    fn with_slot(slot: serde_json::Value) -> String {
        let mut rec = serde_json::json!({"schema": RECORD_SCHEMA, "legal": legal_block(), "source_access": {"state": "not-published"}});
        rec["legal"]["slots"]["LEGAL.COPYRIGHT_LINE"] = slot;
        rec.to_string()
    }

    #[test]
    fn the_new_slot_states_are_accepted_with_their_fixed_tags() {
        let approved = "Approved text body.";
        let draft = "Draft body.";
        for slot in [
            serde_json::json!({"state": "pending", "tag": "Pending a decision", "decisions": ["D-LIC-1"], "pending": ["D-LIC-1"], "text": "Legal text pending decision D-LIC-1. private candidate"}),
            serde_json::json!({"state": "approved-verified", "tag": "Approved text, signature verified", "decisions": [], "pending": [], "text": approved, "text_sha256": sha(approved)}),
            serde_json::json!({"state": "unverified-draft", "tag": "Unverified draft, not an approved text", "decisions": [], "pending": [], "text": format!("{DRAFT_PREFIX}{draft}"), "text_sha256": sha(draft)}),
            serde_json::json!({"state": "not-applicable", "tag": "", "decisions": [], "pending": [], "text": ""}),
        ] {
            assert!(parse_record(&with_slot(slot.clone())).is_ok(), "{slot}");
        }
    }

    #[test]
    fn the_old_states_wrong_tags_and_unlabelled_drafts_are_refused() {
        let bad = [
            serde_json::json!({"state": "placeholder", "decisions": ["D-LIC-1"], "pending": ["D-LIC-1"], "text": "Legal text pending decision D-LIC-1."}),
            serde_json::json!({"state": "filled", "tag": "Approved text, signature verified", "text": "x", "text_sha256": sha("x")}),
            serde_json::json!({"state": "mystery", "tag": "", "text": ""}),
            serde_json::json!({"state": "pending", "tag": "Approved text, signature verified", "text": "Legal text pending decision D-LIC-1."}),
            serde_json::json!({"state": "pending", "text": "Legal text pending decision D-LIC-1."}),
            serde_json::json!({"state": "pending", "tag": "Pending a decision", "text": "some other text"}),
            // a draft without its label inside the text is refused: it would look like ordinary legal text
            serde_json::json!({"state": "unverified-draft", "tag": "Unverified draft, not an approved text", "text": "Draft body.", "text_sha256": sha("Draft body.")}),
            serde_json::json!({"state": "unverified-draft", "tag": "Unverified draft, not an approved text", "text": format!("{DRAFT_PREFIX}Draft body."), "text_sha256": sha("other")}),
            serde_json::json!({"state": "approved-verified", "tag": "Approved text, signature verified", "text": "Approved.", "text_sha256": sha("other")}),
            serde_json::json!({"state": "approved-verified", "tag": "Approved text, signature verified", "text": "Approved."}),
            serde_json::json!({"state": "not-applicable", "tag": "", "text": "text"}),
        ];
        for slot in bad {
            let e = parse_record(&with_slot(slot.clone())).unwrap_err();
            assert_eq!(e.code(), "ABOUT_RECORD_INVALID", "{slot}");
        }
        // source_access outside the two known states, or missing
        let mut rec = serde_json::json!({"schema": RECORD_SCHEMA, "legal": legal_block(), "source_access": {"state": "published"}});
        assert_eq!(parse_record(&rec.to_string()).unwrap_err().code(), "ABOUT_RECORD_INVALID");
        rec.as_object_mut().unwrap().remove("source_access");
        assert_eq!(parse_record(&rec.to_string()).unwrap_err().code(), "ABOUT_RECORD_INVALID");
    }
}
