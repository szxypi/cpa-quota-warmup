package main

import (
	_ "embed"
	"encoding/base64"
)

// logoSVG is the management-console menu icon: a 24x24 rounded square with the
// plugin glyph, matching the built-in console icon spec (viewBox 0 0 24 24,
// rect rx 6, glyph scaled by matrix(0.84 0 0 0.84 1.92 1.92)).
//
// CPA escapes every metadata string with html.EscapeString before sending it to
// the console, so the icon is embedded as a base64 data URI: base64 uses no
// character that HTML escaping rewrites. The console renders it through
// <img src>, where currentColor does not inherit the theme, so the SVG carries
// its own fill colors.
//
//go:embed logo.svg
var logoSVG []byte

// logoDataURI is the Metadata.Logo value.
var logoDataURI = "data:image/svg+xml;base64," + base64.StdEncoding.EncodeToString(logoSVG)
