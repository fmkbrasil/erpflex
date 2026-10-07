package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

type Request struct {
	BaseURL string `json:"base_url"`
	User    string `json:"username"`
	Pass    string `json:"password"`
	Path    string `json:"path"`
	Accept  string `json:"accept"`
}

type Response struct {
	OK          bool             `json:"ok"`
	HTTP        int              `json:"http,omitempty"`
	DurationMS  int64            `json:"duration_ms,omitempty"`
	URL         string           `json:"url,omitempty"`
	Payload     any              `json:"payload,omitempty"`
	Records     []map[string]any `json:"records,omitempty"`
	ArrayPath   string           `json:"array_path,omitempty"`
	BodyHash    string           `json:"body_hash,omitempty"`
	ContentType string           `json:"content_type,omitempty"`
	BodyExcerpt string           `json:"body_excerpt,omitempty"`
	BodyBase64  string           `json:"body_base64,omitempty"`
	Error       string           `json:"error,omitempty"`
}

var httpClient = &http.Client{Transport: &http.Transport{
	Proxy:                 http.ProxyFromEnvironment,
	DialContext:           (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
	ForceAttemptHTTP2:     true,
	MaxIdleConns:          30,
	MaxIdleConnsPerHost:   12,
	IdleConnTimeout:       90 * time.Second,
	TLSHandshakeTimeout:   15 * time.Second,
	ResponseHeaderTimeout: 120 * time.Second,
}}

func extractBestArray(body []byte) ([]map[string]any, string, any) {
	var root any
	if json.Unmarshal(body, &root) != nil {
		return nil, "", nil
	}
	best := []map[string]any{}
	bestPath := ""
	var walk func(any, string)
	walk = func(v any, p string) {
		switch x := v.(type) {
		case []any:
			rows := []map[string]any{}
			for _, it := range x {
				if m, ok := it.(map[string]any); ok {
					rows = append(rows, m)
				}
			}
			if len(rows) > len(best) {
				best = rows
				bestPath = p
			}
			for i, it := range x {
				walk(it, fmt.Sprintf("%s[%d]", p, i))
			}
		case map[string]any:
			for k, it := range x {
				np := "$." + k
				if p != "$" {
					np = p + "." + k
				}
				walk(it, np)
			}
		}
	}
	walk(root, "$")
	return best, bestPath, root
}

func main() {
	dec := json.NewDecoder(io.LimitReader(os.Stdin, 4<<20))
	var in Request
	if err := dec.Decode(&in); err != nil {
		_ = json.NewEncoder(os.Stdout).Encode(Response{OK: false, Error: "entrada inválida: " + err.Error()})
		os.Exit(2)
	}
	in.BaseURL = strings.TrimRight(strings.TrimSpace(in.BaseURL), "/")
	if in.BaseURL == "" || strings.TrimSpace(in.User) == "" || strings.TrimSpace(in.Pass) == "" || strings.TrimSpace(in.Path) == "" {
		_ = json.NewEncoder(os.Stdout).Encode(Response{OK: false, Error: "base_url, username, password e path são obrigatórios"})
		os.Exit(2)
	}
	accept := strings.TrimSpace(in.Accept)
	if accept == "" {
		accept = "application/json"
	}
	path := in.Path
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	url := in.BaseURL + path

	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, url, nil)
	if err != nil {
		_ = json.NewEncoder(os.Stdout).Encode(Response{OK: false, Error: err.Error()})
		os.Exit(2)
	}
	req.SetBasicAuth(in.User, in.Pass)
	req.Header.Set("Accept", accept)
	// Não define User-Agent manualmente: o net/http utiliza Go-http-client/1.1,
	// exatamente como o ERPFlex Analytics V7.8 original.

	started := time.Now()
	resp, err := httpClient.Do(req)
	dur := time.Since(started)
	if err != nil {
		_ = json.NewEncoder(os.Stdout).Encode(Response{OK: false, DurationMS: dur.Milliseconds(), URL: url, Error: err.Error()})
		os.Exit(3)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 20<<20))
	if err != nil {
		_ = json.NewEncoder(os.Stdout).Encode(Response{OK: false, HTTP: resp.StatusCode, DurationMS: dur.Milliseconds(), URL: url, Error: err.Error()})
		os.Exit(4)
	}
	rows, arrayPath, root := extractBestArray(body)
	h := sha256.Sum256(body)
	excerpt := string(body)
	if len(excerpt) > 400 {
		excerpt = excerpt[:400]
	}
	out := Response{
		OK: true, HTTP: resp.StatusCode, DurationMS: dur.Milliseconds(), URL: url,
		Payload: root, Records: rows, ArrayPath: arrayPath,
		BodyHash: hex.EncodeToString(h[:8]), ContentType: resp.Header.Get("Content-Type"), BodyExcerpt: excerpt,
	}
	// Para endpoints não-JSON (ex.: boleto HTML), devolve o corpo integral
	// codificado em base64 para a aplicação poder imprimir/anexar sem truncamento.
	if root == nil || !strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "json") {
		out.BodyBase64 = base64.StdEncoding.EncodeToString(body)
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		os.Exit(5)
	}
}
