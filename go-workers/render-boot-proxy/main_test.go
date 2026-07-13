package main

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testConfig() proxyConfig {
	return proxyConfig{
		publicHost:     "127.0.0.1",
		publicPort:     8080,
		upstreamHost:   "127.0.0.1",
		upstreamPort:   18080,
		restartDelay:   20 * time.Millisecond,
		connectTimeout: 50 * time.Millisecond,
		readLimit:      4096,
	}
}

func environmentMap(entries []string) map[string]string {
	values := map[string]string{}
	for _, entry := range entries {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			values[key] = value
		}
	}
	return values
}

func TestChildEnvironmentPreservesOverridesAndAppliesLiteDefaults(t *testing.T) {
	cfg := testConfig()
	cfg.upstreamPort = 19090
	environment := environmentMap(childEnvironment(cfg, []string{
		"ENABLE_CALENDAR=true",
		"UVICORN_WORKERS=2",
	}))

	if environment["HOST"] != "127.0.0.1" || environment["PORT"] != "19090" {
		t.Fatalf("unexpected upstream environment: %#v", environment)
	}
	if environment["RENDER_BOOT_PROXY"] != "false" {
		t.Fatalf("child proxy recursion was not disabled: %#v", environment)
	}
	if environment["ENABLE_TERMINAL_SERVERS"] != "false" {
		t.Fatalf("terminal servers must default to disabled: %#v", environment)
	}
	if environment["ENABLE_CALENDAR"] != "true" || environment["UVICORN_WORKERS"] != "2" {
		t.Fatalf("explicit environment values must be preserved: %#v", environment)
	}
}

func TestUnavailableHealthReturnsReadyFallback(t *testing.T) {
	response := unavailableResponse(t, "GET /health HTTP/1.1\r\nHost: test\r\n\r\n")
	if !strings.Contains(response, "HTTP/1.1 200 OK") || !strings.Contains(response, `"upstream_ready":false`) {
		t.Fatalf("unexpected health fallback response: %q", response)
	}
}

func TestUnavailableRequestReturnsChinese503(t *testing.T) {
	response := unavailableResponse(t, "GET / HTTP/1.1\r\nHost: test\r\n\r\n")
	if !strings.Contains(response, "HTTP/1.1 503 Service Unavailable") || !strings.Contains(response, "正在启动") {
		t.Fatalf("unexpected startup response: %q", response)
	}
}

func unavailableResponse(t *testing.T, request string) string {
	t.Helper()
	server, client := net.Pipe()
	proxy := newBootProxy(testConfig())
	proxy.dial = func(context.Context, string, string) (net.Conn, error) {
		return nil, errors.New("upstream unavailable")
	}
	done := make(chan struct{})
	go func() {
		proxy.handleClient(context.Background(), server)
		close(done)
	}()
	if _, err := io.WriteString(client, request); err != nil {
		t.Fatal(err)
	}
	response, err := io.ReadAll(client)
	if err != nil {
		t.Fatal(err)
	}
	<-done
	return string(response)
}

func TestProxyForwardsHTTPBidirectionally(t *testing.T) {
	upstream, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer upstream.Close()

	upstreamDone := make(chan struct{})
	go func() {
		defer close(upstreamDone)
		connection, acceptError := upstream.Accept()
		if acceptError != nil {
			return
		}
		defer connection.Close()
		request, _ := bufio.NewReader(connection).ReadString('\n')
		if strings.HasPrefix(request, "GET /stream ") {
			_, _ = io.WriteString(connection, "HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\npong")
		}
	}()

	proxyListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := testConfig()
	cfg.upstreamPort = upstream.Addr().(*net.TCPAddr).Port
	proxyDone := make(chan struct{})
	go func() {
		_ = newBootProxy(cfg).serve(ctx, proxyListener)
		close(proxyDone)
	}()
	client, err := net.Dial("tcp", proxyListener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.WriteString(client, "GET /stream HTTP/1.1\r\nHost: test\r\n\r\n")
	_ = client.(*net.TCPConn).CloseWrite()
	response, err := io.ReadAll(client)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(string(response), "pong") {
		t.Fatalf("unexpected proxied response: %q", response)
	}
	_ = client.Close()
	cancel()
	<-proxyDone
	<-upstreamDone
}

func TestSupervisorRestartsAndStopsChild(t *testing.T) {
	tempDirectory := t.TempDir()
	marker := filepath.Join(tempDirectory, "starts")
	cfg := testConfig()
	cfg.childCommand = "printf 'start\\n' >> " + marker + "; sleep 0.02; exit 7"
	supervisor := newChildSupervisor(cfg)
	ctx, cancel := context.WithCancel(context.Background())
	go supervisor.run(ctx)

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		contents, _ := os.ReadFile(marker)
		if strings.Count(string(contents), "start") >= 2 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	contents, _ := os.ReadFile(marker)
	if strings.Count(string(contents), "start") < 2 {
		cancel()
		supervisor.stop(time.Second)
		<-supervisor.done
		t.Fatalf("child was not restarted: %q", contents)
	}

	cancel()
	supervisor.stop(time.Second)
	select {
	case <-supervisor.done:
	case <-time.After(2 * time.Second):
		t.Fatal("supervisor did not stop after cancellation")
	}
}
