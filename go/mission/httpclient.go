package mission

// Dependency-free HTTP with an injectable transport. The kernel stays stdlib-only: postJSON uses
// net/http by default, but every client accepts a Transport so tests inject a fake and no network
// (or live GPU / Dagster) is needed to prove the wiring. Go port of agentic_os/mission/util.py.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Transport is (url, body, headers, timeout) → parsed JSON object; a fake is injected in tests.
type Transport func(url string, body map[string]any, headers map[string]string, timeout float64) (map[string]any, error)

// HTTPError is a non-2xx / unreachable response.
type HTTPError struct{ Msg string }

func (e *HTTPError) Error() string { return e.Msg }

func urllibTransport(url string, body map[string]any, headers map[string]string, timeout float64) (map[string]any, error) {
	data, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := (&http.Client{Timeout: time.Duration(timeout * float64(time.Second))}).Do(req)
	if err != nil {
		return nil, &HTTPError{Msg: "unreachable: " + err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		s := string(raw)
		if len(s) > 300 {
			s = s[:300]
		}
		return nil, &HTTPError{Msg: fmt.Sprintf("http_%d: %s", resp.StatusCode, s)}
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return map[string]any{}, nil
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// postJSON POSTs a JSON body via the given transport (default net/http).
func postJSON(url string, body map[string]any, headers map[string]string, timeout float64, transport Transport) (map[string]any, error) {
	if transport == nil {
		transport = urllibTransport
	}
	if headers == nil {
		headers = map[string]string{}
	}
	return transport(url, body, headers, timeout)
}
