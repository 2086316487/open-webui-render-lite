package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

// The HF Storage Buckets S3 gateway routes GetObject through a CDN path for
// non-SDK clients and returns 401 there, so the client must present a
// botocore-style User-Agent (see plan doc section 22.2).
const s3UserAgent = "Boto3/1.40.0 Python/3.12 Botocore/1.40.0"

const s3ErrorBodyLimit = 4 * 1024

type s3Result struct {
	OK        bool     `json:"ok"`
	Op        string   `json:"op"`
	Status    int      `json:"status,omitempty"`
	Key       string   `json:"key,omitempty"`
	Bytes     int64    `json:"bytes,omitempty"`
	Keys      []string `json:"keys,omitempty"`
	Truncated bool     `json:"truncated,omitempty"`
	ErrorCode string   `json:"error_code,omitempty"`
	Message   string   `json:"message,omitempty"`
}

type s3Options struct {
	op        string
	endpoint  string
	region    string
	bucket    string
	key       string
	file      string
	prefix    string
	maxKeys   int
	timeout   time.Duration
	accessKey string
	secretKey string
}

func runS3(args []string) {
	opts, err := parseS3Options(args)
	if err != nil {
		writeS3Result(s3Result{OK: false, Op: opts.op, ErrorCode: "invalid_arguments", Message: err.Error()})
		return
	}

	var res s3Result
	switch opts.op {
	case "put":
		res = s3Put(opts)
	case "get":
		res = s3Get(opts)
	case "delete":
		res = s3Delete(opts)
	case "list":
		res = s3List(opts)
	default:
		res = s3Result{OK: false, Op: opts.op, ErrorCode: "invalid_arguments", Message: "unsupported --op"}
	}
	writeS3Result(res)
}

func parseS3Options(args []string) (s3Options, error) {
	fs := flag.NewFlagSet("s3", flag.ContinueOnError)
	fs.SetOutput(io.Discard)

	opts := s3Options{}
	fs.StringVar(&opts.op, "op", "", "put|get|delete|list")
	fs.StringVar(&opts.endpoint, "endpoint", "", "S3 endpoint URL, may include a path namespace")
	fs.StringVar(&opts.region, "region", "us-east-1", "signing region")
	fs.StringVar(&opts.bucket, "bucket", "", "bucket name")
	fs.StringVar(&opts.key, "key", "", "object key")
	fs.StringVar(&opts.file, "file", "", "local file path for put/get")
	fs.StringVar(&opts.prefix, "prefix", "", "key prefix for list")
	fs.IntVar(&opts.maxKeys, "max-keys", 1000, "max keys for list")
	timeoutSeconds := fs.Int("timeout-seconds", 60, "per-request timeout")

	if err := fs.Parse(args); err != nil {
		return opts, err
	}
	opts.timeout = time.Duration(*timeoutSeconds) * time.Second

	// Credentials come from the environment, never from argv (visible in /proc).
	opts.accessKey = os.Getenv("S3_ACCESS_KEY_ID")
	opts.secretKey = os.Getenv("S3_SECRET_ACCESS_KEY")

	if opts.op == "" || opts.endpoint == "" || opts.bucket == "" {
		return opts, fmt.Errorf("--op, --endpoint and --bucket are required")
	}
	if opts.accessKey == "" || opts.secretKey == "" {
		return opts, fmt.Errorf("S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY not set in environment")
	}
	if (opts.op == "put" || opts.op == "get" || opts.op == "delete") && opts.key == "" {
		return opts, fmt.Errorf("--key is required for %s", opts.op)
	}
	if (opts.op == "put" || opts.op == "get") && opts.file == "" {
		return opts, fmt.Errorf("--file is required for %s", opts.op)
	}
	return opts, nil
}

func s3Put(opts s3Options) s3Result {
	body, err := os.ReadFile(opts.file)
	if err != nil {
		return s3Result{OK: false, Op: "put", Key: opts.key, ErrorCode: "local_read_failed", Message: err.Error()}
	}
	status, respBody, err := s3Request(opts, "PUT", opts.key, nil, body, nil)
	if err != nil {
		return s3Result{OK: false, Op: "put", Key: opts.key, ErrorCode: "s3_request_failed", Message: err.Error()}
	}
	if status != 200 && status != 201 {
		return s3FailureResult("put", opts.key, status, respBody)
	}
	return s3Result{OK: true, Op: "put", Status: status, Key: opts.key, Bytes: int64(len(body))}
}

func s3Get(opts s3Options) s3Result {
	out, err := os.Create(opts.file)
	if err != nil {
		return s3Result{OK: false, Op: "get", Key: opts.key, ErrorCode: "local_write_failed", Message: err.Error()}
	}
	status, respBody, written, err := s3RequestToFile(opts, opts.key, out)
	closeErr := out.Close()
	if err != nil || status != 200 {
		os.Remove(opts.file)
		if err != nil {
			return s3Result{OK: false, Op: "get", Key: opts.key, ErrorCode: "s3_request_failed", Message: err.Error()}
		}
		return s3FailureResult("get", opts.key, status, respBody)
	}
	if closeErr != nil {
		os.Remove(opts.file)
		return s3Result{OK: false, Op: "get", Key: opts.key, ErrorCode: "local_write_failed", Message: closeErr.Error()}
	}
	return s3Result{OK: true, Op: "get", Status: status, Key: opts.key, Bytes: written}
}

func s3Delete(opts s3Options) s3Result {
	status, respBody, err := s3Request(opts, "DELETE", opts.key, nil, nil, nil)
	if err != nil {
		return s3Result{OK: false, Op: "delete", Key: opts.key, ErrorCode: "s3_request_failed", Message: err.Error()}
	}
	if status != 200 && status != 202 && status != 204 {
		return s3FailureResult("delete", opts.key, status, respBody)
	}
	return s3Result{OK: true, Op: "delete", Status: status, Key: opts.key}
}

type s3ListBucketResult struct {
	IsTruncated bool `xml:"IsTruncated"`
	Contents    []struct {
		Key string `xml:"Key"`
	} `xml:"Contents"`
}

func s3List(opts s3Options) s3Result {
	query := map[string]string{
		"list-type": "2",
		"max-keys":  strconv.Itoa(opts.maxKeys),
	}
	if opts.prefix != "" {
		query["prefix"] = opts.prefix
	}
	status, respBody, err := s3Request(opts, "GET", "", query, nil, nil)
	if err != nil {
		return s3Result{OK: false, Op: "list", ErrorCode: "s3_request_failed", Message: err.Error()}
	}
	if status != 200 {
		return s3FailureResult("list", "", status, respBody)
	}
	var parsed s3ListBucketResult
	if err := xml.Unmarshal(respBody, &parsed); err != nil {
		return s3Result{OK: false, Op: "list", Status: status, ErrorCode: "s3_list_parse_failed", Message: err.Error()}
	}
	keys := make([]string, 0, len(parsed.Contents))
	for _, item := range parsed.Contents {
		keys = append(keys, item.Key)
	}
	return s3Result{OK: true, Op: "list", Status: status, Keys: keys, Truncated: parsed.IsTruncated}
}

func s3FailureResult(op, key string, status int, body []byte) s3Result {
	message := strings.TrimSpace(string(body))
	if len(message) > 300 {
		message = message[:300]
	}
	return s3Result{
		OK:        false,
		Op:        op,
		Status:    status,
		Key:       key,
		ErrorCode: fmt.Sprintf("s3_%s_failed", op),
		Message:   message,
	}
}

// s3Request performs a signed request and returns status plus the (limited) body.
func s3Request(opts s3Options, method, key string, query map[string]string, body []byte, bodyLimitOverride *int64) (int, []byte, error) {
	req, err := buildSignedS3Request(opts, method, key, query, body, time.Now())
	if err != nil {
		return 0, nil, err
	}

	client := &http.Client{Timeout: opts.timeout}
	resp, err := doWithRetry(client, req, body)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()

	limit := int64(s3ErrorBodyLimit)
	if method == "GET" && key == "" {
		// list responses carry the full XML payload
		limit = 4 * 1024 * 1024
	}
	if bodyLimitOverride != nil {
		limit = *bodyLimitOverride
	}
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, limit))
	return resp.StatusCode, respBody, nil
}

// s3RequestToFile streams a GET response body directly into out.
func s3RequestToFile(opts s3Options, key string, out io.Writer) (int, []byte, int64, error) {
	req, err := buildSignedS3Request(opts, "GET", key, nil, nil, time.Now())
	if err != nil {
		return 0, nil, 0, err
	}

	client := &http.Client{Timeout: opts.timeout}
	resp, err := doWithRetry(client, req, nil)
	if err != nil {
		return 0, nil, 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, s3ErrorBodyLimit))
		return resp.StatusCode, respBody, 0, nil
	}
	written, err := io.Copy(out, resp.Body)
	if err != nil {
		return resp.StatusCode, nil, written, err
	}
	return resp.StatusCode, nil, written, nil
}

// doWithRetry retries once on transport errors and 5xx, mirroring boto3's retry cushion.
func doWithRetry(client *http.Client, req *http.Request, body []byte) (*http.Response, error) {
	resp, err := client.Do(req)
	if err == nil && resp.StatusCode < 500 {
		return resp, nil
	}
	if resp != nil {
		io.Copy(io.Discard, io.LimitReader(resp.Body, s3ErrorBodyLimit))
		resp.Body.Close()
	}
	time.Sleep(500 * time.Millisecond)
	retryReq := req.Clone(req.Context())
	if body != nil {
		retryReq.Body = io.NopCloser(strings.NewReader(string(body)))
		retryReq.ContentLength = int64(len(body))
	}
	return client.Do(retryReq)
}

func buildSignedS3Request(opts s3Options, method, key string, query map[string]string, body []byte, now time.Time) (*http.Request, error) {
	endpointURL, err := url.Parse(strings.TrimSuffix(opts.endpoint, "/"))
	if err != nil {
		return nil, fmt.Errorf("invalid endpoint: %w", err)
	}
	if endpointURL.Scheme == "" || endpointURL.Host == "" {
		return nil, fmt.Errorf("invalid endpoint: %s", opts.endpoint)
	}

	// Path-style addressing: <endpoint-path>/<bucket>[/<key>]
	rawPath := strings.TrimSuffix(endpointURL.Path, "/") + "/" + opts.bucket
	if key != "" {
		rawPath += "/" + key
	}
	canonicalURI := s3EncodePath(rawPath)
	canonicalQ := s3CanonicalQuery(query)

	fullURL := endpointURL.Scheme + "://" + endpointURL.Host + canonicalURI
	if canonicalQ != "" {
		fullURL += "?" + canonicalQ
	}

	var bodyReader io.Reader
	if body != nil {
		bodyReader = strings.NewReader(string(body))
	}
	req, err := http.NewRequest(method, fullURL, bodyReader)
	if err != nil {
		return nil, err
	}

	payloadHash := sha256Hex(body)
	amzDate := now.UTC().Format("20060102T150405Z")
	canonicalHeaders := "host:" + endpointURL.Host + "\n" +
		"x-amz-content-sha256:" + payloadHash + "\n" +
		"x-amz-date:" + amzDate + "\n"
	signedHeaders := "host;x-amz-content-sha256;x-amz-date"

	authorization := buildAuthorization(
		method, canonicalURI, canonicalQ, canonicalHeaders, signedHeaders,
		payloadHash, opts.accessKey, opts.secretKey, opts.region, "s3", amzDate,
	)

	req.Header.Set("Authorization", authorization)
	req.Header.Set("User-Agent", s3UserAgent)
	req.Header.Set("x-amz-date", amzDate)
	req.Header.Set("x-amz-content-sha256", payloadHash)
	return req, nil
}

func buildAuthorization(method, canonicalURI, canonicalQuery, canonicalHeaders, signedHeaders, payloadHash, accessKey, secret, region, service, amzDate string) string {
	dateStamp := amzDate[:8]
	canonicalRequest := strings.Join(
		[]string{method, canonicalURI, canonicalQuery, canonicalHeaders, signedHeaders, payloadHash},
		"\n",
	)
	scope := dateStamp + "/" + region + "/" + service + "/aws4_request"
	stringToSign := strings.Join(
		[]string{"AWS4-HMAC-SHA256", amzDate, scope, sha256Hex([]byte(canonicalRequest))},
		"\n",
	)
	signature := hex.EncodeToString(hmacSHA256(signingKey(secret, dateStamp, region, service), stringToSign))
	return "AWS4-HMAC-SHA256 Credential=" + accessKey + "/" + scope +
		", SignedHeaders=" + signedHeaders + ", Signature=" + signature
}

func signingKey(secret, dateStamp, region, service string) []byte {
	kDate := hmacSHA256([]byte("AWS4"+secret), dateStamp)
	kRegion := hmacSHA256(kDate, region)
	kService := hmacSHA256(kRegion, service)
	return hmacSHA256(kService, "aws4_request")
}

func hmacSHA256(key []byte, data string) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(data))
	return mac.Sum(nil)
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// s3EncodePath percent-encodes every path segment per RFC 3986 while keeping
// the '/' separators, matching the AWS SigV4 canonical URI rules for S3.
func s3EncodePath(p string) string {
	segments := strings.Split(p, "/")
	for i, segment := range segments {
		segments[i] = s3EncodeSegment(segment)
	}
	return strings.Join(segments, "/")
}

func s3EncodeSegment(s string) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
			c == '-' || c == '.' || c == '_' || c == '~' {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func s3CanonicalQuery(params map[string]string) string {
	if len(params) == 0 {
		return ""
	}
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, s3EncodeSegment(k)+"="+s3EncodeSegment(params[k]))
	}
	return strings.Join(parts, "&")
}

func writeS3Result(res s3Result) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(res); err != nil {
		fmt.Fprintln(os.Stdout, `{"ok":false,"op":"","error_code":"json_encode_failed","message":"failed to encode result"}`)
	}
}
