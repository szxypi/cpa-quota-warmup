package main

import (
	"encoding/base64"
	"encoding/json"
	"encoding/xml"
	"io"
	"strings"
	"testing"
)

// TestLogoDataURI guards the contract between the plugin Logo field and the
// management console: the value must be a base64 SVG data URI (so the host's
// html.EscapeString leaves it intact), it must decode to well-formed XML, and
// the registration returned to CPA must carry it.
func TestLogoDataURI(t *testing.T) {
	const prefix = "data:image/svg+xml;base64,"
	if !strings.HasPrefix(logoDataURI, prefix) {
		t.Fatalf("logo is not a base64 SVG data URI: %.40q", logoDataURI)
	}
	if strings.ContainsAny(logoDataURI, "<>&'\"") {
		t.Fatalf("logo contains a character the host HTML-escapes: %q", logoDataURI)
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(logoDataURI, prefix))
	if err != nil {
		t.Fatalf("logo is not valid base64: %v", err)
	}
	if !strings.Contains(string(raw), "<svg") {
		t.Fatalf("decoded logo is not an SVG document")
	}
	dec := xml.NewDecoder(strings.NewReader(string(raw)))
	var root xml.Name
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("decoded logo is not valid XML: %v", err)
		}
		if start, ok := tok.(xml.StartElement); ok && root.Local == "" {
			root = start.Name
		}
	}
	if root.Local != "svg" {
		t.Fatalf("decoded logo root element = %q, want %q", root.Local, "svg")
	}

	payload, err := json.Marshal(registrationPayload())
	if err != nil {
		t.Fatalf("marshal registration: %v", err)
	}
	var reg struct {
		Metadata struct {
			Logo string `json:"Logo"`
		} `json:"metadata"`
	}
	if err := json.Unmarshal(payload, &reg); err != nil {
		t.Fatalf("unmarshal registration: %v", err)
	}
	if reg.Metadata.Logo != logoDataURI {
		t.Fatalf("registration metadata logo does not match the embedded icon")
	}
}
