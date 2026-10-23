package mission

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func TestFileReplayGuardFirstUseWinsAndReplayIsRefused(t *testing.T) {
	g, err := NewFileReplayGuard(filepath.Join(t.TempDir(), "r"), 0)
	if err != nil {
		t.Fatal(err)
	}
	if !g.Consume("n1", 2000, 1000) || g.Consume("n1", 2000, 1001) || !g.Consume("n2", 2000, 1001) {
		t.Fatal("first use must win, replay must be refused")
	}
}

func TestFileReplayGuardSurvivesARestart(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "r")
	a, _ := NewFileReplayGuard(dir, 0)
	a.Consume("n1", 2000, 1000)
	b, _ := NewFileReplayGuard(dir, 0) // a new process / restarted operator
	if b.Consume("n1", 2000, 1500) {
		t.Fatal("a restart must not forget a used nonce")
	}
}

func TestFileReplayGuardPurgesExpiredAndFailsClosedWhenFull(t *testing.T) {
	g, _ := NewFileReplayGuard(filepath.Join(t.TempDir(), "r"), 2)
	if !g.Consume("a", 1100, 1000) || !g.Consume("b", 1100, 1000) {
		t.Fatal("fill")
	}
	if g.Consume("c", 1100, 1000) {
		t.Fatal("full of live nonces: fail closed")
	}
	if !g.Consume("c", 3000, 1200) {
		t.Fatal("expired entries should have been purged")
	}
}

func TestFileReplayGuardDirectoryIsPrivate(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "r")
	g, _ := NewFileReplayGuard(dir, 0)
	g.Consume("n", 2000, 1000)
	st, _ := os.Stat(dir)
	if st.Mode().Perm()&0o077 != 0 {
		t.Fatalf("directory mode %v", st.Mode())
	}
}

func TestConcurrentConsumersExactlyOneWins(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "r")
	var wins int32
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() { // separate guard instances model separate workers over the same directory
			defer wg.Done()
			g, _ := NewFileReplayGuard(dir, 0)
			if g.Consume("shared", 9e12, 1) {
				atomic.AddInt32(&wins, 1)
			}
		}()
	}
	wg.Wait()
	if wins != 1 {
		t.Fatalf("%d winners", wins)
	}
}

// Separate OS processes: the helper below re-executes the test binary.
func TestTwoProcessesCannotBothConsumeTheSameNonce(t *testing.T) {
	if os.Getenv("REPLAY_CHILD_DIR") != "" {
		g, _ := NewFileReplayGuard(os.Getenv("REPLAY_CHILD_DIR"), 0)
		if g.Consume("shared", 9e12, 1) {
			os.Exit(0)
		}
		os.Exit(3)
	}
	dir := filepath.Join(t.TempDir(), "r")
	var wins int32
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			cmd := exec.Command(os.Args[0], "-test.run=TestTwoProcessesCannotBothConsumeTheSameNonce")
			cmd.Env = append(os.Environ(), "REPLAY_CHILD_DIR="+dir)
			if cmd.Run() == nil {
				atomic.AddInt32(&wins, 1)
			}
		}()
	}
	wg.Wait()
	if wins != 1 {
		t.Fatalf("%d processes consumed the nonce", wins)
	}
}

func TestProductionModeRefusesToStartWithoutADurableStore(t *testing.T) {
	t.Setenv(EnvReplayDB, "")
	t.Setenv(EnvRequireDurableReplay, "1")
	if _, err := DefaultReplayGuard(); err != ErrReplayNotDurable {
		t.Fatalf("got %v", err)
	}
	if _, err := NewOperatorE("x", nil); err != ErrReplayNotDurable {
		t.Fatalf("NewOperatorE: %v", err)
	}
	defer func() {
		if recover() == nil {
			t.Fatal("NewOperator must refuse to start (panic) rather than downgrade silently")
		}
	}()
	NewOperator("x", nil)
}

func TestProductionModeWithADirectoryStartsAndUsesIt(t *testing.T) {
	t.Setenv(EnvReplayDB, filepath.Join(t.TempDir(), "r"))
	t.Setenv(EnvRequireDurableReplay, "1")
	op := NewOperator("x", nil)
	if _, ok := op.ReplayGuard.(*FileReplayGuard); !ok {
		t.Fatalf("operator guard is %T", op.ReplayGuard)
	}
}

func TestDefaultIsPerProcessWithoutTheFlag(t *testing.T) {
	t.Setenv(EnvReplayDB, "")
	t.Setenv(EnvRequireDurableReplay, "")
	g, err := DefaultReplayGuard()
	if err != nil || strings.Contains(strings.ToLower(os.Getenv(EnvReplayDB)), "x") {
		t.Fatal("default should work")
	}
	if _, ok := g.(*ReplayGuard); !ok {
		t.Fatalf("default guard is %T", g)
	}
}

func TestIdempotencyKeyReachesAKeyedHandler(t *testing.T) {
	var got []string
	op := NewOperator("billing", []Capability{NewKeyedCapability("billing.charge",
		func(in map[string]any, key string) (map[string]any, error) {
			got = append(got, key)
			return map[string]any{"charged": true}, nil
		})})
	if _, err := op.Invoke("billing.charge", map[string]any{"amount": 1}, "k-123"); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "k-123" {
		t.Fatalf("handler saw keys %v", got)
	}
	// a plain Handler capability still works unchanged
	plain := NewOperator("x", []Capability{NewCapability("x.do", func(in map[string]any) (map[string]any, error) {
		return map[string]any{"ok": true}, nil
	})})
	if _, err := plain.Invoke("x.do", map[string]any{}, "k"); err != nil {
		t.Fatal(err)
	}
}
