// Package executor handles JSON-RPC method dispatch for Evonet.
package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// cacheTTL is how long a completed request's result is retained for idempotent
// replay. The Evonic server only re-sends a request id within its disconnect
// grace window (currently 90s), so a few minutes is comfortably safe.
const cacheTTL = 5 * time.Minute

// inflight tracks a request that is either still executing or recently completed.
// done is closed once resp is populated; concurrent callers with the same id wait
// on done and then read resp (exactly-once execution).
type inflight struct {
	done chan struct{}
	resp Response
}

// Request is an inbound JSON-RPC command from the Evonic server.
type Request struct {
	ID     string          `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

// Response is the reply sent back to Evonic.
type Response struct {
	ID     string `json:"id"`
	OK     bool   `json:"ok"`
	Result any    `json:"result,omitempty"`
	Error  string `json:"error,omitempty"`
}

// Executor dispatches incoming requests to method handlers.
type Executor struct {
	workDir string
	verbose bool

	mu    sync.Mutex
	cache map[string]*inflight // req.ID → in-flight/completed result

	// Cached login-shell environment captured once at startup.
	// On macOS this recovers PATH, custom env vars, and toolchain
	// directories that GUI-launched processes don't normally inherit.
	loginEnv     []string
	loginEnvOnce sync.Once
}

func New(workDir string, verbose bool) *Executor {
	// Normalize: clean, ensure trailing separator, reject root and empty.
	clean := filepath.Clean(workDir)
	if clean == "/" || clean == "." || clean == "" {
		panic("evonet: workDir must not be root, empty, or current directory")
	}
	if !strings.HasSuffix(clean, string(os.PathSeparator)) {
		clean += string(os.PathSeparator)
	}
	return &Executor{workDir: clean, verbose: verbose, cache: make(map[string]*inflight)}
}

// getEnviron returns the cached login-shell environment if available,
// falling back to the process environment. RPC-supplied env vars are
// merged on top by the callers (handleExecBash / handleExecPython).
func (e *Executor) getEnviron() []string {
	e.loginEnvOnce.Do(func() {
		e.loginEnv = captureLoginEnv()
	})
	if e.loginEnv != nil {
		return e.loginEnv
	}
	return os.Environ()
}

// envCaptureMarker delimits the start of `env` output. The user's login
// shell may emit shell-integration escape codes or other preamble on stdout
// while sourcing rc files; everything up to and including this marker line is
// discarded so only real KEY=VALUE pairs remain.
const envCaptureMarker = "__EVONET_ENV_START__"

// captureLoginEnv recovers the full interactive login-shell environment
// (PATH, toolchain dirs, custom vars). It uses the user's actual login shell
// ($SHELL, e.g. zsh on macOS) — not a hardcoded bash — in login+interactive
// mode so that both profile files (~/.zprofile, ~/.profile) and rc files
// (~/.zshrc) are sourced. This matters on macOS, where GUI/launchd-launched
// processes inherit a bare environment and where env lives in zsh config that
// `bash -l` never reads. Returns nil on failure so callers gracefully fall
// back to the process environment.
func captureLoginEnv() []string {
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/bash"
	}

	// login + interactive so both profile and rc files are sourced.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, shell, "-l", "-i", "-c", "echo "+envCaptureMarker+"; env")
	// Detach from the controlling terminal: an interactive shell would otherwise
	// grab the TTY's foreground process group and leave evonet unable to receive
	// CTRL+C after the first command (Linux bash ≥4.4 / zsh).
	detachTTY(cmd)
	out, err := cmd.Output()
	if err != nil {
		log.Printf("[evonet] login env capture failed (shell=%s, env will be process env): %v", shell, err)
		return nil
	}

	lines := strings.Split(string(out), "\n")
	// Discard everything up to and including the marker line (shell-integration
	// escape codes, prompt preamble, etc. emitted while rc files were sourced).
	start := 0
	for i, line := range lines {
		if strings.Contains(line, envCaptureMarker) {
			start = i + 1
			break
		}
	}
	filtered := make([]string, 0, len(lines))
	for _, line := range lines[start:] {
		if strings.Contains(line, "=") {
			filtered = append(filtered, line)
		}
	}
	if len(filtered) == 0 {
		log.Printf("[evonet] login env capture produced no vars (shell=%s); using process env", shell)
		return nil
	}
	return filtered
}

// Handle processes a Request and returns a Response.
//
// It guarantees exactly-once execution per request id: if the same id is seen
// again while still running, the second caller waits for and returns the first
// result; if it already completed (within cacheTTL), the cached result is
// replayed without re-executing. This lets the Evonic server safely re-send a
// request after a WebSocket reconnect without risking duplicate side effects.
func (e *Executor) Handle(req Request) Response {
	// Requests without an id can't be deduplicated — dispatch directly.
	if req.ID == "" {
		return e.dispatch(req)
	}

	e.mu.Lock()
	if existing, ok := e.cache[req.ID]; ok {
		e.mu.Unlock()
		<-existing.done // attach to in-flight or already-completed execution
		return existing.resp
	}
	entry := &inflight{done: make(chan struct{})}
	e.cache[req.ID] = entry
	e.mu.Unlock()

	entry.resp = e.dispatch(req)
	close(entry.done)
	e.scheduleEvict(req.ID)
	return entry.resp
}

// scheduleEvict removes a cached result after cacheTTL so memory stays bounded.
func (e *Executor) scheduleEvict(id string) {
	time.AfterFunc(cacheTTL, func() {
		e.mu.Lock()
		delete(e.cache, id)
		e.mu.Unlock()
	})
}

// dispatch routes a request to its method handler.
func (e *Executor) dispatch(req Request) Response {
	if e.verbose {
		e.logRequest(req)
	}
	start := time.Now()

	var resp Response
	switch req.Method {
	case "exec_bash":
		resp = e.handleExecBash(req)
	case "exec_python":
		resp = e.handleExecPython(req)
	case "read_file":
		resp = e.handleReadFile(req)
	case "write_file":
		resp = e.handleWriteFile(req)
	case "read_file_b64":
		resp = e.handleReadFileB64(req)
	case "write_file_b64":
		resp = e.handleWriteFileB64(req)
	default:
		resp = Response{ID: req.ID, OK: false, Error: fmt.Sprintf("unknown method: %s", req.Method)}
	}

	if e.verbose {
		e.logResponse(req.Method, resp, time.Since(start))
	}
	return resp
}

func (e *Executor) logRequest(req Request) {
	switch req.Method {
	case "exec_bash":
		var p execBashParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → exec_bash\n%s", scriptSnippet(p.Script))
	case "exec_python":
		var p execPythonParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → exec_python\n%s", scriptSnippet(p.Code))
	case "read_file":
		var p readFileParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → read_file: %s", p.Path)
	case "write_file":
		var p writeFileParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → write_file: %s (%s, %d bytes)", p.Path, p.Mode, len(p.Content))
	case "read_file_b64":
		var p readFileB64Params
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → read_file_b64: %s (offset=%d, size=%d)", p.Path, p.Offset, p.Size)
	case "write_file_b64":
		var p writeFileB64Params
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → write_file_b64: %s (offset=%d, is_last=%v, %d b64 bytes)", p.Path, p.Offset, p.IsLast, len(p.Data))
	default:
		log.Printf("[evonet] → %s", req.Method)
	}
}

func (e *Executor) logResponse(method string, resp Response, dur time.Duration) {
	if !resp.OK {
		log.Printf("[evonet] ← %s ERROR: %s (%.2fs)", method, resp.Error, dur.Seconds())
		return
	}
	switch method {
	case "exec_bash", "exec_python":
		if r, ok := resp.Result.(execResult); ok {
			out := r.Stdout
			if out == "" {
				out = r.Stderr
			}
			log.Printf("[evonet] ← %s OK (exit %d, %.2fs)%s",
				method, r.ExitCode, dur.Seconds(), inlineIfNotEmpty(truncateN(out, 120)))
			return
		}
	case "read_file":
		if r, ok := resp.Result.(map[string]any); ok {
			log.Printf("[evonet] ← read_file OK (%v bytes, %.2fs)", r["size"], dur.Seconds())
			return
		}
	}
	log.Printf("[evonet] ← %s OK (%.2fs)", method, dur.Seconds())
}

// scriptSnippet returns the first 3 lines of a script, truncated to 200 chars total.
func scriptSnippet(s string) string {
	s = strings.TrimSpace(s)
	lines := strings.SplitN(s, "\n", 4)
	if len(lines) > 3 {
		lines = append(lines[:3], "...")
	}
	out := strings.Join(lines, "\n")
	return truncateN(out, 200)
}

func truncateN(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func inlineIfNotEmpty(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	return "\n          " + strings.ReplaceAll(s, "\n", "\n          ")
}

func errResp(id, msg string) Response {
	return Response{ID: id, OK: false, Error: msg}
}

func okResp(id string, result any) Response {
	return Response{ID: id, OK: true, Result: result}
}
