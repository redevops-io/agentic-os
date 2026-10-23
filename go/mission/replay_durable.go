package mission

// Durable cross-process replay protection for approval nonces. Go analogue of SqliteReplayGuard in
// agentic_os/mission/approval.py; same Consume contract, same environment variables, a different store.
//
// The store is a DIRECTORY (AGENTIC_OS_REPLAY_DB names it, not a SQLite file): one small file per used nonce, created
// with O_CREATE|O_EXCL, which is atomic across processes on a local filesystem and needs no dependency. A Go operator
// and a Python operator therefore do not share one replay store: each operator verifies the tokens addressed to it
// (the token binds the operator name), so that is sufficient per operator; multi-HOST deployments (or one logical
// operator served by both languages) need a shared store behind the same Consume contract (Redis/Postgres).
//
// Production: AGENTIC_OS_REQUIRE_DURABLE_REPLAY=1 makes NewOperator refuse to start without a durable store instead of
// silently falling back to the per-process set that a restart or a second worker forgets.

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	EnvReplayDB             = "AGENTIC_OS_REPLAY_DB"
	EnvRequireDurableReplay = "AGENTIC_OS_REQUIRE_DURABLE_REPLAY"
)

// NonceGuard is the replay contract: true the first time a nonce is seen, false on replay (and when it cannot tell).
type NonceGuard interface {
	Consume(nonce string, exp, now float64) bool
}

// ErrReplayNotDurable: durable replay protection is required but no durable store is configured.
var ErrReplayNotDurable = errors.New(EnvRequireDurableReplay + " is set but " + EnvReplayDB +
	" is not: approval replay protection would be per-process (set it to a private directory on durable storage)")

// FileReplayGuard keeps one file per used nonce (named by the nonce's sha256) under a private directory.
type FileReplayGuard struct {
	dir string
	max int
	mu  sync.Mutex
}

// NewFileReplayGuard opens (creating, 0700) the directory. maxEntries <= 0 means 100000.
func NewFileReplayGuard(dir string, maxEntries int) (*FileReplayGuard, error) {
	if maxEntries <= 0 {
		maxEntries = 100_000
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	_ = os.Chmod(dir, 0o700)
	return &FileReplayGuard{dir: dir, max: maxEntries}, nil
}

func (g *FileReplayGuard) path(nonce string) string {
	sum := sha256.Sum256([]byte(nonce))
	return filepath.Join(g.dir, hex.EncodeToString(sum[:]))
}

// Consume is true only for the process that creates the nonce's file. Expired entries are purged opportunistically;
// a full table or any storage error fails closed (false).
func (g *FileReplayGuard) Consume(nonce string, exp, now float64) bool {
	if now == 0 {
		now = float64(time.Now().UnixNano()) / 1e9
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	entries, err := os.ReadDir(g.dir)
	if err != nil {
		return false
	}
	if len(entries) >= g.max/2 {
		g.purge(entries, now)
		if entries, err = os.ReadDir(g.dir); err != nil || len(entries) >= g.max {
			return false
		}
	}
	f, err := os.OpenFile(g.path(nonce), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return false // already used (EEXIST) or unwritable: never "allowed"
	}
	_, werr := f.WriteString(strconv.FormatFloat(exp, 'f', -1, 64))
	cerr := f.Close()
	if werr != nil || cerr != nil {
		_ = os.Remove(g.path(nonce))
		return false
	}
	return true
}

func (g *FileReplayGuard) purge(entries []os.DirEntry, now float64) {
	for _, e := range entries {
		p := filepath.Join(g.dir, e.Name())
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		if exp, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64); err == nil && exp <= now {
			_ = os.Remove(p)
		}
	}
}

var warnedNonDurable sync.Once

// DefaultReplayGuard is the guard an Operator uses: durable when AGENTIC_OS_REPLAY_DB names a directory, else the
// per-process set (a warning is logged once). With AGENTIC_OS_REQUIRE_DURABLE_REPLAY=1 and no directory it returns
// ErrReplayNotDurable.
func DefaultReplayGuard() (NonceGuard, error) {
	if dir := strings.TrimSpace(os.Getenv(EnvReplayDB)); dir != "" {
		g, err := NewFileReplayGuard(dir, 0)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", EnvReplayDB, err)
		}
		return g, nil
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(EnvRequireDurableReplay))) {
	case "1", "true", "yes", "on":
		return nil, ErrReplayNotDurable
	}
	warnedNonDurable.Do(func() {
		log.Printf("agentic-os: approval replay protection is per-process (%s unset): a restart or a second worker forgets "+
			"used approvals. Set %s for production and %s=1 to refuse to start without it.",
			EnvReplayDB, EnvReplayDB, EnvRequireDurableReplay)
	})
	return NewReplayGuard(0), nil
}
