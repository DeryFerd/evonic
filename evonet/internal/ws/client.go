// Package ws provides the WebSocket client that connects Evonet to the Evonic server.
package ws

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/evonic/evonet/internal/config"
	"github.com/evonic/evonet/internal/executor"
	"github.com/evonic/evonet/internal/version"
	"github.com/gorilla/websocket"
)

// Heartbeat tuning. The active heartbeat only runs while one or more requests
// are in flight, so an idle fleet of connectors produces no ping traffic.
const (
	heartbeatInterval = 10 * time.Second
	heartbeatTimeout  = 25 * time.Second // ~2 missed pings → declare the link dead
)

// Client manages the WebSocket connection to the Evonic connector relay.
type Client struct {
	cfg            *config.Config
	exec           *executor.Executor
	conn           *websocket.Conn
	mu             sync.Mutex
	running        atomic.Bool
	stopCh         chan struct{}
	stopOnce       sync.Once
	inflight       atomic.Int64 // requests currently executing / awaiting reply
	lastRecv       atomic.Int64 // unixnano of the last frame received
	OnConnected    func()       // called after successful connect (from Run's goroutine)
	OnDisconnected func()       // called when message loop ends while still running (retrying)
}

func New(cfg *config.Config, exec *executor.Executor) *Client {
	return &Client{
		cfg:    cfg,
		exec:   exec,
		stopCh: make(chan struct{}),
	}
}

// Run connects and runs the message loop, reconnecting on failure with exponential
// backoff. Blocks until Stop() is called.
func (c *Client) Run() {
	c.running.Store(true)
	backoff := 1.0
	for c.running.Load() {
		connectedAt := time.Now()
		if err := c.connect(); err != nil {
			log.Printf("[evonet] Connection failed: %v", err)
		} else {
			log.Printf("[evonet] Connected to %s (home: %s)", c.cfg.ServerURL, c.cfg.HomeName)
			if c.OnConnected != nil {
				c.OnConnected()
			}
			if err := c.messageLoop(); err != nil {
				log.Printf("[evonet] Disconnected: %v", err)
			}
			// Only fire OnDisconnected if we are going to retry (not user-initiated stop)
			if c.running.Load() && c.OnDisconnected != nil {
				c.OnDisconnected()
			}
			// Reset backoff if the connection was healthy for more than 10s
			if time.Since(connectedAt) > 10*time.Second {
				backoff = 1.0
			}
		}
		if !c.running.Load() {
			break
		}
		// Add ±20% jitter to avoid thundering herd
		jitter := 1.0 + (0.4*float64(time.Now().UnixNano()%100)/100.0 - 0.2)
		wait := time.Duration(backoff*jitter*1000) * time.Millisecond
		if wait > 30*time.Second {
			wait = 30 * time.Second
		}
		log.Printf("[evonet] Reconnecting in %.1fs...", wait.Seconds())
		select {
		case <-time.After(wait):
		case <-c.stopCh:
			return
		}
		backoff = math.Min(backoff*2, 30)
	}
}

// RunOnce is an alias for Run — always reconnects with backoff.
// A one-shot connect that dies on disconnect is not useful in practice.
func (c *Client) RunOnce() error {
	c.Run()
	return nil
}

// Stop signals the client to disconnect and stop reconnecting.
func (c *Client) Stop() {
	c.running.Store(false)
	c.stopOnce.Do(func() { close(c.stopCh) })
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != nil {
		c.conn.Close()
	}
}

func (c *Client) wsURL() string {
	server := strings.TrimRight(c.cfg.ServerURL, "/")
	// Map http(s):// → ws(s):// and append the connector path
	server = strings.Replace(server, "https://", "wss://", 1)
	server = strings.Replace(server, "http://", "ws://", 1)
	return server + "/ws/connector"
}

func (c *Client) connect() error {
	url := c.wsURL()
	header := http.Header{}
	header.Set("Authorization", "Bearer "+c.cfg.ConnectorToken)
	header.Set("User-Agent", "Evonet/1.0")

	hostname, _ := os.Hostname()
	header.Set("X-Device-Name", hostname)
	header.Set("X-Platform", runtime.GOOS)
	header.Set("X-Evonet-Version", version.Version)
	// Advertise that this client deduplicates requests by id, so the server may
	// safely re-send an in-flight request after a reconnect (exactly-once).
	header.Set("X-Evonet-Caps", "idempotent-replay")

	conn, _, err := websocket.DefaultDialer.Dial(url, header)
	if err != nil {
		return fmt.Errorf("dial %s: %w", url, err)
	}
	conn.SetReadLimit(512 * 1024) // 512KB for base64 chunks + JSON wrapper
	c.mu.Lock()
	c.conn = conn
	c.mu.Unlock()
	return nil
}

func (c *Client) messageLoop() error {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()

	// Ping/pong keepalive
	conn.SetPingHandler(func(data string) error {
		return c.safeWrite(conn, websocket.PongMessage, []byte(data))
	})

	// Active heartbeat: while requests are in flight, proactively ping and force
	// a reconnect if the link goes silent (detects half-open TCP fast so the
	// server can replay the request well inside its disconnect grace window).
	c.lastRecv.Store(time.Now().UnixNano())
	stopHB := make(chan struct{})
	defer close(stopHB)
	go c.heartbeat(conn, stopHB)

	for {
		_, raw, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		c.lastRecv.Store(time.Now().UnixNano())

		// Control frames ({"type":"ping"|"pong"}) are valid JSON that would also
		// unmarshal into an empty executor.Request, so detect them explicitly
		// before treating a frame as an RPC request.
		var env struct {
			Type   string `json:"type"`
			Method string `json:"method"`
		}
		if json.Unmarshal(raw, &env) != nil {
			continue // malformed frame
		}
		switch env.Type {
		case "ping":
			pong, _ := json.Marshal(map[string]string{"type": "pong"})
			if err := c.safeWrite(conn, websocket.TextMessage, pong); err != nil {
				return err
			}
			continue
		case "pong":
			continue // liveness already recorded above
		}
		if env.Method == "" {
			continue // not a request we understand
		}

		var req executor.Request
		if err := json.Unmarshal(raw, &req); err != nil {
			continue
		}

		// Handle request in goroutine so we don't block
		c.inflight.Add(1)
		go func(r executor.Request) {
			defer c.inflight.Add(-1)
			resp := c.exec.Handle(r)
			data, err := json.Marshal(resp)
			if err != nil {
				return
			}
			// On any send failure the result stays in the executor cache, so the
			// server recovers it by re-sending the same request id after reconnect.
			if err := c.safeWrite(conn, websocket.TextMessage, data); err != nil {
				log.Printf("[evonet] result for %s not sent (%v); will replay on reconnect", r.ID, err)
			}
		}(req)
	}
}

// heartbeat runs for the lifetime of one connection. It only does work while
// requests are in flight: it pings the server and, if no frame has been received
// within heartbeatTimeout, closes the connection to force a reconnect. When idle
// it stays completely silent, so a large fleet of connectors generates no traffic.
func (c *Client) heartbeat(conn *websocket.Conn, stop <-chan struct{}) {
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()
	ping, _ := json.Marshal(map[string]string{"type": "ping"})
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			if c.inflight.Load() == 0 {
				continue // no in-flight work → no pinging
			}
			if since := time.Since(time.Unix(0, c.lastRecv.Load())); since > heartbeatTimeout {
				log.Printf("[evonet] link silent for %.0fs with %d in-flight; forcing reconnect",
					since.Seconds(), c.inflight.Load())
				conn.Close() // unblocks ReadMessage → messageLoop returns → reconnect
				return
			}
			if err := c.safeWrite(conn, websocket.TextMessage, ping); err != nil {
				conn.Close()
				return // connection replaced or write failed; this heartbeat is done
			}
		}
	}
}

// safeWrite serializes all writes to the connection. gorilla/websocket permits
// only one concurrent writer, and the read loop, response goroutines and the
// heartbeat can all write. It also no-ops if conn is no longer the active one.
func (c *Client) safeWrite(conn *websocket.Conn, messageType int, data []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != conn {
		return errors.New("connection superseded")
	}
	return conn.WriteMessage(messageType, data)
}
