// About / Legal data for the launcher window (RC3, U-03). Two read-only commands that serve FIXED files of
// the application's own `Licenses` resource folder to the launcher page. Properties, each covered by a test:
//   * the page never supplies a path: `about_text` takes a name that must equal one of six constants, and the
//     file opened is built from the constant, never from the input;
//   * the record `about.json` and each text have a size limit, are read through a bounded reader and must be
//     regular files (a symlink or directory of the same name is refused);
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

/// Read a regular file `dir/name` (name is a constant of this module) into a string, bounded by `cap`.
fn read_capped(dir: &Path, name: &str, cap: u64, missing: AboutError, large: &'static str) -> Result<String, AboutError> {
    let path = dir.join(name);
    // symlink_metadata: a symlink (even one pointing at a regular file) is refused, so a planted link
    // cannot make this view read a file outside the Licenses folder.
    let meta = match std::fs::symlink_metadata(&path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(missing),
        Err(e) => return Err(AboutError::Io(e.kind())),
    };
    if !meta.is_file() {
        return Err(missing);
    }
    if meta.len() > cap {
        return Err(AboutError::TooLarge(large));
    }
    let file = std::fs::File::open(&path).map_err(|e| AboutError::Io(e.kind()))?;
    // Bounded again at read time (the file may grow between stat and read).
    let mut buf = Vec::new();
    file.take(cap + 1).read_to_end(&mut buf).map_err(|e| AboutError::Io(e.kind()))?;
    if buf.len() as u64 > cap {
        return Err(AboutError::TooLarge(large));
    }
    String::from_utf8(buf).map_err(|_| AboutError::NotText)
}

/// Parse and minimally validate the record text. The page validates the shape in more detail; this is the
/// gate that keeps a malformed or foreign file from being handed over as "the build record".
fn parse_record(text: &str) -> Result<serde_json::Value, AboutError> {
    let v: serde_json::Value =
        serde_json::from_str(text).map_err(|_| AboutError::RecordInvalid("it is not valid JSON"))?;
    if !v.is_object() {
        return Err(AboutError::RecordInvalid("it is not a JSON object"));
    }
    if v.get("schema").and_then(|s| s.as_str()) != Some(RECORD_SCHEMA) {
        return Err(AboutError::RecordInvalid("its schema is not redevops.about/1"));
    }
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
        let rec = serde_json::json!({"schema": RECORD_SCHEMA, "licence_files": [{"file": "LICENSE", "sha256": sha(&body)}]});
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
        assert!(code.contains("pub async fn about_info(app: tauri::AppHandle)"));
        assert!(code.contains("pub async fn about_text(app: tauri::AppHandle, slug: String)"));
    }
}
