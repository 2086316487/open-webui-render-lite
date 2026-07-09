package main

import (
	"encoding/hex"
	"strings"
	"testing"
	"time"
)

// Official AWS "deriving the signing key" example:
// https://docs.aws.amazon.com/general/latest/gr/sigv4-calculate-signature.html
func TestSigningKeyAWSExample(t *testing.T) {
	key := signingKey("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830", "us-east-1", "iam")
	got := hex.EncodeToString(key)
	want := "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"
	if got != want {
		t.Fatalf("signing key mismatch:\n got %s\nwant %s", got, want)
	}
}

func TestSha256HexEmpty(t *testing.T) {
	want := "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	if got := sha256Hex(nil); got != want {
		t.Fatalf("empty payload hash mismatch: got %s", got)
	}
}

// "get-vanilla" case from the official AWS SigV4 test suite.
func TestBuildAuthorizationGetVanilla(t *testing.T) {
	emptyHash := sha256Hex(nil)
	canonicalHeaders := "host:example.amazonaws.com\nx-amz-date:20150830T123600Z\n"
	auth := buildAuthorization(
		"GET", "/", "", canonicalHeaders, "host;x-amz-date",
		emptyHash,
		"AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
		"us-east-1", "service", "20150830T123600Z",
	)
	wantSignature := "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
	if !strings.HasSuffix(auth, "Signature="+wantSignature) {
		t.Fatalf("authorization mismatch:\n got %s", auth)
	}
	wantPrefix := "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, SignedHeaders=host;x-amz-date, "
	if !strings.HasPrefix(auth, wantPrefix) {
		t.Fatalf("authorization prefix mismatch:\n got %s", auth)
	}
}

func TestS3EncodePath(t *testing.T) {
	cases := map[string]string{
		"/ns/bucket/render-lite/plain.txt":     "/ns/bucket/render-lite/plain.txt",
		"/ns/bucket/render-lite/my file.txt":   "/ns/bucket/render-lite/my%20file.txt",
		"/ns/bucket/render-lite/a+b&c=d.txt":   "/ns/bucket/render-lite/a%2Bb%26c%3Dd.txt",
		"/ns/bucket/render-lite/uuid_文件.txt": "/ns/bucket/render-lite/uuid_%E6%96%87%E4%BB%B6.txt",
		"/ns/bucket/keep~-._chars":             "/ns/bucket/keep~-._chars",
	}
	for input, want := range cases {
		if got := s3EncodePath(input); got != want {
			t.Fatalf("s3EncodePath(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestS3CanonicalQuery(t *testing.T) {
	got := s3CanonicalQuery(map[string]string{
		"prefix":    "render-lite/",
		"list-type": "2",
		"max-keys":  "1000",
	})
	want := "list-type=2&max-keys=1000&prefix=render-lite%2F"
	if got != want {
		t.Fatalf("canonical query mismatch:\n got %s\nwant %s", got, want)
	}
}

// The wire request must carry exactly the headers and path that were signed.
func TestBuildSignedS3RequestShape(t *testing.T) {
	opts := s3Options{
		op:        "put",
		endpoint:  "https://s3.hf.co/g2086316487",
		region:    "us-east-1",
		bucket:    "open-webui-files",
		key:       "render-lite/id_my file.txt",
		accessKey: "AKIDEXAMPLE",
		secretKey: "secret",
		timeout:   10 * time.Second,
	}
	now := time.Date(2026, 7, 9, 12, 0, 0, 0, time.UTC)
	req, err := buildSignedS3Request(opts, "PUT", opts.key, nil, []byte("hello"), now)
	if err != nil {
		t.Fatalf("buildSignedS3Request failed: %v", err)
	}
	wantPath := "/g2086316487/open-webui-files/render-lite/id_my%20file.txt"
	if got := req.URL.EscapedPath(); got != wantPath {
		t.Fatalf("wire path mismatch:\n got %s\nwant %s", got, wantPath)
	}
	if req.Host != "s3.hf.co" && req.URL.Host != "s3.hf.co" {
		t.Fatalf("host mismatch: %s / %s", req.Host, req.URL.Host)
	}
	if req.Header.Get("x-amz-content-sha256") != sha256Hex([]byte("hello")) {
		t.Fatalf("payload hash header mismatch")
	}
	if req.Header.Get("x-amz-date") != "20260709T120000Z" {
		t.Fatalf("amz-date mismatch: %s", req.Header.Get("x-amz-date"))
	}
	if ua := req.Header.Get("User-Agent"); !strings.Contains(ua, "Botocore") {
		t.Fatalf("user-agent must stay botocore-compatible for the HF gateway, got %q", ua)
	}
	if auth := req.Header.Get("Authorization"); !strings.Contains(auth, "SignedHeaders=host;x-amz-content-sha256;x-amz-date") {
		t.Fatalf("unexpected signed headers: %s", auth)
	}
}
