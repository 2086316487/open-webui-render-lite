package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	defaultPublicPort     = 8080
	defaultUpstreamPort   = 18080
	defaultRestartDelay   = 5 * time.Second
	defaultConnectTimeout = 250 * time.Millisecond
	defaultReadLimit      = 64 * 1024
	childShutdownTimeout  = 15 * time.Second
)

type proxyConfig struct {
	publicHost     string
	publicPort     int
	upstreamHost   string
	upstreamPort   int
	restartDelay   time.Duration
	connectTimeout time.Duration
	readLimit      int
	childCommand   string
}

func loadConfig() proxyConfig {
	return proxyConfig{
		publicHost:     envString("HOST", "0.0.0.0"),
		publicPort:     envInt("PORT", defaultPublicPort),
		upstreamHost:   envString("OPEN_WEBUI_INTERNAL_HOST", "127.0.0.1"),
		upstreamPort:   envInt("OPEN_WEBUI_INTERNAL_PORT", defaultUpstreamPort),
		restartDelay:   envDurationSeconds("RENDER_BOOT_PROXY_RESTART_DELAY", defaultRestartDelay),
		connectTimeout: envDurationSeconds("RENDER_BOOT_PROXY_CONNECT_TIMEOUT", defaultConnectTimeout),
		readLimit:      envInt("RENDER_BOOT_PROXY_READ_LIMIT", defaultReadLimit),
		childCommand:   strings.TrimSpace(os.Getenv("RENDER_BOOT_PROXY_CHILD_CMD")),
	}
}

func envString(key, fallback string) string {
	if value, ok := os.LookupEnv(key); ok && value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envDurationSeconds(key string, fallback time.Duration) time.Duration {
	value, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(key)), 64)
	if err != nil || value <= 0 {
		return fallback
	}
	return time.Duration(value * float64(time.Second))
}

func (cfg proxyConfig) publicAddress() string {
	return net.JoinHostPort(cfg.publicHost, strconv.Itoa(cfg.publicPort))
}

func (cfg proxyConfig) upstreamAddress() string {
	return net.JoinHostPort(cfg.upstreamHost, strconv.Itoa(cfg.upstreamPort))
}

func childEnvironment(cfg proxyConfig, source []string) []string {
	values := make(map[string]string, len(source)+16)
	for _, entry := range source {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			values[key] = value
		}
	}

	values["HOST"] = cfg.upstreamHost
	values["PORT"] = strconv.Itoa(cfg.upstreamPort)
	values["RENDER_BOOT_PROXY"] = "false"
	defaults := map[string]string{
		"OPEN_WEBUI_LITE_MODE":  "true",
		"UVICORN_WORKERS":       "1",
		"PYTHONDONTWRITEBYTECODE": "1",
		"MALLOC_ARENA_MAX":      "1",
		"OMP_NUM_THREADS":       "1",
		"OPENBLAS_NUM_THREADS":  "1",
		"MKL_NUM_THREADS":       "1",
		"NUMEXPR_NUM_THREADS":   "1",
		"TOKENIZERS_PARALLELISM": "false",
		"ENABLE_TERMINAL_SERVERS": "false",
		"ENABLE_AUTOMATIONS":    "false",
		"ENABLE_CALENDAR":       "false",
	}
	for key, value := range defaults {
		if _, exists := values[key]; !exists {
			values[key] = value
		}
	}

	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	environment := make([]string, 0, len(keys))
	for _, key := range keys {
		environment = append(environment, key+"="+values[key])
	}
	return environment
}

func childProcess(cfg proxyConfig) (*exec.Cmd, string) {
	if cfg.childCommand != "" {
		return exec.Command("/bin/sh", "-c", cfg.childCommand), cfg.childCommand
	}
	return exec.Command("bash", "start.sh"), "bash start.sh"
}

type runningChild struct {
	cmd  *exec.Cmd
	done chan struct{}
}

type childSupervisor struct {
	cfg     proxyConfig
	mu      sync.Mutex
	current *runningChild
	done    chan struct{}
}

func newChildSupervisor(cfg proxyConfig) *childSupervisor {
	return &childSupervisor{cfg: cfg, done: make(chan struct{})}
}

func (supervisor *childSupervisor) run(ctx context.Context) {
	defer close(supervisor.done)

	for ctx.Err() == nil {
		cmd, display := childProcess(supervisor.cfg)
		cmd.Env = childEnvironment(supervisor.cfg, os.Environ())
		cmd.Stdin = os.Stdin
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

		log.Printf("starting child: %s", display)
		if err := cmd.Start(); err != nil {
			log.Printf("failed to start child: %v", err)
			if !waitForRestart(ctx, supervisor.cfg.restartDelay) {
				return
			}
			continue
		}

		child := &runningChild{cmd: cmd, done: make(chan struct{})}
		supervisor.mu.Lock()
		supervisor.current = child
		supervisor.mu.Unlock()

		err := cmd.Wait()
		close(child.done)
		supervisor.mu.Lock()
		if supervisor.current == child {
			supervisor.current = nil
		}
		supervisor.mu.Unlock()

		if err != nil {
			log.Printf("child exited: %v", err)
		} else {
			log.Printf("child exited with code 0")
		}
		if ctx.Err() != nil {
			return
		}
		log.Printf("restarting child in %s", supervisor.cfg.restartDelay)
		if !waitForRestart(ctx, supervisor.cfg.restartDelay) {
			return
		}
	}
}

func waitForRestart(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (supervisor *childSupervisor) stop(timeout time.Duration) {
	supervisor.mu.Lock()
	child := supervisor.current
	supervisor.mu.Unlock()
	if child == nil || child.cmd.Process == nil {
		return
	}

	log.Printf("stopping child process group %d", child.cmd.Process.Pid)
	_ = syscall.Kill(-child.cmd.Process.Pid, syscall.SIGTERM)
	select {
	case <-child.done:
		return
	case <-time.After(timeout):
		log.Printf("child did not stop within %s; killing process group", timeout)
		_ = syscall.Kill(-child.cmd.Process.Pid, syscall.SIGKILL)
		<-child.done
	}
}

type bootProxy struct {
	cfg  proxyConfig
	dial func(context.Context, string, string) (net.Conn, error)
}

func newBootProxy(cfg proxyConfig) *bootProxy {
	dialer := &net.Dialer{Timeout: cfg.connectTimeout}
	return &bootProxy{cfg: cfg, dial: dialer.DialContext}
}

func (proxy *bootProxy) serve(ctx context.Context, listener net.Listener) error {
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()

	for {
		client, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		go proxy.handleClient(ctx, client)
	}
}

func (proxy *bootProxy) handleClient(ctx context.Context, client net.Conn) {
	upstream, err := proxy.dial(ctx, "tcp", proxy.cfg.upstreamAddress())
	if err != nil {
		proxy.handleUnavailable(client)
		return
	}
	defer upstream.Close()
	defer client.Close()

	var wait sync.WaitGroup
	wait.Add(2)
	go func() {
		defer wait.Done()
		copyConnection(upstream, client, proxy.cfg.readLimit)
	}()
	go func() {
		defer wait.Done()
		copyConnection(client, upstream, proxy.cfg.readLimit)
	}()
	wait.Wait()
}

func copyConnection(destination, source net.Conn, bufferSize int) {
	if bufferSize <= 0 {
		bufferSize = defaultReadLimit
	}
	_, _ = io.CopyBuffer(destination, source, make([]byte, bufferSize))
	if halfCloser, ok := destination.(interface{ CloseWrite() error }); ok {
		_ = halfCloser.CloseWrite()
	}
}

func (proxy *bootProxy) handleUnavailable(client net.Conn) {
	defer client.Close()
	_ = client.SetReadDeadline(time.Now().Add(time.Second))
	request := make([]byte, 4096)
	count, _ := client.Read(request)
	_ = client.SetReadDeadline(time.Time{})

	if bytes.HasPrefix(request[:count], []byte("GET /health ")) || bytes.HasPrefix(request[:count], []byte("HEAD /health ")) {
		writeHTTPResponse(client, "200 OK", "application/json", []byte("{\"status\":true,\"upstream_ready\":false}\n"))
		return
	}
	writeHTTPResponse(
		client,
		"503 Service Unavailable",
		"text/plain; charset=utf-8",
		[]byte("Open WebUI 正在启动，请稍后刷新页面。\n"),
	)
}

func writeHTTPResponse(writer io.Writer, status, contentType string, body []byte) {
	_, _ = fmt.Fprintf(
		writer,
		"HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n",
		status,
		contentType,
		len(body),
	)
	_, _ = writer.Write(body)
}

func run() error {
	cfg := loadConfig()
	listener, err := net.Listen("tcp", cfg.publicAddress())
	if err != nil {
		return err
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	supervisor := newChildSupervisor(cfg)
	go supervisor.run(ctx)

	log.Printf("listening on %s, forwarding to %s", listener.Addr(), cfg.upstreamAddress())
	serveError := newBootProxy(cfg).serve(ctx, listener)
	cancel()
	supervisor.stop(childShutdownTimeout)
	<-supervisor.done
	return serveError
}

func main() {
	log.SetPrefix("[render_boot_proxy_go] ")
	log.SetFlags(0)
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
