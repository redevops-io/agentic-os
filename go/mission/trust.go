package mission

// Capability trust boundary (Go port of trust.py). A capability whose Source is a third party is an
// execution surface; the Context Runtime treats untrusted sources like a plugin host: they stay
// DISCOVERABLE (the registry returns them) but are NOT bindable until the source is trusted — the
// bind door fails closed. Built-in capabilities (Source == "") are trusted, so nothing changes.

import (
	"bufio"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// TrustStore is the set of trusted capability sources, persisted one-per-line.
type TrustStore struct {
	Path    string
	trusted map[string]bool
}

func trustDefaultPath() string {
	if p := os.Getenv("AGENTIC_OS_TRUST_FILE"); p != "" {
		return p
	}
	home := os.Getenv("AGENTIC_OS_HOME")
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	return filepath.Join(home, ".agentic-os", "trusted-sources")
}

// NewTrustStore loads the store from path (default AGENTIC_OS_TRUST_FILE/~/.agentic-os/trusted-sources).
func NewTrustStore(path string) *TrustStore {
	if path == "" {
		path = trustDefaultPath()
	}
	ts := &TrustStore{Path: path, trusted: map[string]bool{}}
	if f, err := os.Open(path); err == nil {
		defer f.Close()
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			if s := strings.TrimSpace(sc.Text()); s != "" {
				ts.trusted[s] = true
			}
		}
	}
	return ts
}

// TrustStoreWith builds an in-memory store from an explicit set (no disk) — for tests/embedding.
func TrustStoreWith(sources ...string) *TrustStore {
	ts := &TrustStore{trusted: map[string]bool{}}
	for _, s := range sources {
		ts.trusted[s] = true
	}
	return ts
}

// IsTrusted: built-in/unsourced ("") is trusted; otherwise the source must be trusted.
func (ts *TrustStore) IsTrusted(source string) bool {
	return source == "" || ts.trusted[source]
}

func (ts *TrustStore) Trust(source string) {
	if source != "" {
		ts.trusted[source] = true
	}
}

func (ts *TrustStore) Revoke(source string) { delete(ts.trusted, source) }

func (ts *TrustStore) Save() error {
	if ts.Path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(ts.Path), 0o755); err != nil {
		return err
	}
	keys := make([]string, 0, len(ts.trusted))
	for k := range ts.trusted {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return os.WriteFile(ts.Path, []byte(strings.Join(keys, "\n")+"\n"), 0o644)
}
