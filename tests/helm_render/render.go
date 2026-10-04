// Corey Mathie, 2026
//
// A minimal Helm-template renderer for tests, using only Go's standard library (text/template, the
// engine Helm uses). It implements the subset of Helm and Sprig functions that
// deploy/helm/local-llm-gateway uses, with stricter missing-key handling than Helm. It is NOT helm:
// it doesn't validate against Kubernetes schemas, and toYaml is a small block-style encoder (strings
// always double-quoted) rather than the YAML library Helm uses.
//
// Usage: go run render.go <chart-dir> <input.json>
// input.json: {"Values": {...merged values...}, "Chart": {...}, "Release": {...}}
// Output (stdout): JSON {"templates/x.yaml": "rendered text", ...}; errors exit 1 with the message.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"text/template"
)

func toJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(b)
}

var plainKey = regexp.MustCompile(`^[A-Za-z0-9_./-]+$`)

func yamlKey(k string) string {
	if plainKey.MatchString(k) {
		return k
	}
	return toJSON(k)
}

func isEmptyOrScalar(v any) bool {
	switch x := v.(type) {
	case map[string]any:
		return len(x) == 0
	case []any:
		return len(x) == 0
	}
	return true
}

// toYAML: block style like Helm's toYaml (no trailing newline); scalars as JSON (valid YAML).
func toYAML(v any) string {
	switch x := v.(type) {
	case map[string]any:
		if len(x) == 0 {
			return "{}"
		}
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var lines []string
		for _, k := range keys {
			child := x[k]
			switch {
			case isEmptyOrScalar(child):
				lines = append(lines, yamlKey(k)+": "+toYAML(child))
			case isList(child):
				lines = append(lines, yamlKey(k)+":", toYAML(child))
			default:
				lines = append(lines, yamlKey(k)+":", indent(2, toYAML(child)))
			}
		}
		return strings.Join(lines, "\n")
	case []any:
		if len(x) == 0 {
			return "[]"
		}
		var lines []string
		for _, item := range x {
			body := toYAML(item)
			lines = append(lines, "- "+strings.ReplaceAll(body, "\n", "\n  "))
		}
		return strings.Join(lines, "\n")
	}
	return toJSON(v)
}

func isList(v any) bool {
	_, ok := v.([]any)
	return ok
}

func indent(n int, s string) string {
	pad := strings.Repeat(" ", n)
	return pad + strings.ReplaceAll(s, "\n", "\n"+pad)
}

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case string:
		return x != ""
	case bool:
		return x
	case float64:
		return x != 0
	case int:
		return x != 0
	case map[string]any:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: render <chart-dir> <input.json>")
		os.Exit(2)
	}
	chart, input := os.Args[1], os.Args[2]
	raw, err := os.ReadFile(input)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	data["Template"] = map[string]any{"BasePath": "templates"}

	root := template.New("chart").Option("missingkey=error")
	funcs := template.FuncMap{
		"include": func(name string, d any) (string, error) {
			var buf bytes.Buffer
			err := root.ExecuteTemplate(&buf, name, d)
			return buf.String(), err
		},
		"default": func(def any, v ...any) any {
			if len(v) == 0 || !truthy(v[0]) {
				return def
			}
			return v[0]
		},
		"required": func(msg string, v any) (any, error) {
			if !truthy(v) {
				return nil, errors.New(msg)
			}
			return v, nil
		},
		"fail": func(msg string) (string, error) { return "", errors.New(msg) },
		"trunc": func(n int, s string) string {
			if len(s) > n {
				return s[:n]
			}
			return s
		},
		"trimSuffix": func(suf, s string) string { return strings.TrimSuffix(s, suf) },
		"contains":   func(sub, s string) bool { return strings.Contains(s, sub) },
		"replace":    func(old, new, s string) string { return strings.ReplaceAll(s, old, new) },
		"quote":      func(v any) string { return fmt.Sprintf("%q", fmt.Sprint(v)) },
		"nindent":    func(n int, s string) string { return "\n" + indent(n, s) },
		"indent":     indent,
		"toYaml":     toYAML,
		"toJson":     toJSON,
		"sha256sum": func(s string) string {
			h := sha256.Sum256([]byte(s))
			return hex.EncodeToString(h[:])
		},
		"int": func(v any) int {
			switch x := v.(type) {
			case float64:
				return int(x)
			case int:
				return x
			}
			return 0
		},
	}
	root.Funcs(funcs)

	var files []string
	_ = filepath.Walk(filepath.Join(chart, "templates"), func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			files = append(files, p)
		}
		return nil
	})
	sort.Strings(files)
	for _, f := range files {
		body, err := os.ReadFile(f)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		rel, _ := filepath.Rel(chart, f)
		if _, err := root.New(filepath.ToSlash(rel)).Parse(string(body)); err != nil {
			fmt.Fprintln(os.Stderr, "parse:", err)
			os.Exit(1)
		}
	}
	out := map[string]string{}
	for _, f := range files {
		rel, _ := filepath.Rel(chart, f)
		name := filepath.ToSlash(rel)
		if strings.HasPrefix(filepath.Base(name), "_") {
			continue
		}
		var buf bytes.Buffer
		if err := root.ExecuteTemplate(&buf, name, data); err != nil {
			fmt.Fprintln(os.Stderr, "render:", err)
			os.Exit(1)
		}
		out[name] = buf.String()
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", " ")
	_ = enc.Encode(out)
}
