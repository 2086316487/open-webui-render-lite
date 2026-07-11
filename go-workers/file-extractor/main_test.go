package main

import (
	"archive/zip"
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func writeZipFixture(t *testing.T, name string, files map[string]string) string {
	t.Helper()
	fixturePath := filepath.Join(t.TempDir(), name)
	output, err := os.Create(fixturePath)
	if err != nil {
		t.Fatalf("create fixture: %v", err)
	}

	writer := zip.NewWriter(output)
	names := make([]string, 0, len(files))
	for filename := range files {
		names = append(names, filename)
	}
	sort.Strings(names)
	for _, filename := range names {
		entry, err := writer.Create(filename)
		if err != nil {
			t.Fatalf("create zip entry %s: %v", filename, err)
		}
		if _, err := entry.Write([]byte(files[filename])); err != nil {
			t.Fatalf("write zip entry %s: %v", filename, err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close zip writer: %v", err)
	}
	if err := output.Close(); err != nil {
		t.Fatalf("close fixture: %v", err)
	}
	return fixturePath
}

func officeOptions(input string, filename string, contentType string) options {
	return options{
		input:          input,
		filename:       filename,
		contentType:    contentType,
		officeMaxBytes: 2 * 1024 * 1024,
		officeMaxChars: 128 * 1024,
		maxSheets:      5,
		maxRows:        500,
	}
}

func TestExtractDocxTablesAndLinks(t *testing.T) {
	input := writeZipFixture(t, "quality.docx", map[string]string{
		"word/document.xml": `<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
<w:p><w:r><w:t>Visit </w:t></w:r><w:hyperlink r:id="rIdLink"><w:r><w:t>Example</w:t></w:r></w:hyperlink></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>Alpha</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>`,
		"word/_rels/document.xml.rels": `<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/docs" TargetMode="External"/></Relationships>`,
	})

	base := result{ParserVersion: parserVersion, Format: "docx", Category: "office", Warnings: []string{}}
	got := extractDocx(officeOptions(input, "quality.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), base)
	if !got.OK {
		t.Fatalf("extract docx failed: %+v", got)
	}
	for _, expected := range []string{
		"Visit Example (https://example.com/docs)",
		"[Table]",
		"| Name | Value |",
		"| Alpha | 42 |",
	} {
		if !strings.Contains(got.Text, expected) {
			t.Fatalf("docx output missing %q:\n%s", expected, got.Text)
		}
	}
}

func TestExtractXlsxCoordinatesAndFormulas(t *testing.T) {
	input := writeZipFixture(t, "quality.xlsx", map[string]string{
		"xl/workbook.xml": `<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Data" sheetId="1"/></sheets></workbook>`,
		"xl/sharedStrings.xml": `<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Header</t></si></sst>`,
		"xl/worksheets/sheet1.xml": `<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c></row>
<row r="2"><c r="B2"><v>2</v></c><c r="C2"><f>B2+3</f><v>5</v></c><c r="D2" t="b"><v>1</v></c><c r="E2" t="inlineStr"><is><t>Ready</t></is></c></row>
</sheetData></worksheet>`,
	})

	base := result{ParserVersion: parserVersion, Format: "xlsx", Category: "office", Warnings: []string{}}
	got := extractXlsx(officeOptions(input, "quality.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), base)
	if !got.OK {
		t.Fatalf("extract xlsx failed: %+v", got)
	}
	for _, expected := range []string{
		"[Sheet: Data]",
		"A1=Header",
		"B2=2 | C2=5 (formula: =B2+3) | D2=TRUE | E2=Ready",
	} {
		if !strings.Contains(got.Text, expected) {
			t.Fatalf("xlsx output missing %q:\n%s", expected, got.Text)
		}
	}
}

func TestExtractPptxSpeakerNotes(t *testing.T) {
	input := writeZipFixture(t, "quality.pptx", map[string]string{
		"ppt/slides/slide1.xml": `<?xml version="1.0" encoding="UTF-8"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Quarterly Review</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>`,
		"ppt/slides/_rels/slide1.xml.rels": `<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>`,
		"ppt/notesSlides/notesSlide1.xml": `<?xml version="1.0" encoding="UTF-8"?><p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Emphasize year-over-year growth.</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>`,
	})

	base := result{ParserVersion: parserVersion, Format: "pptx", Category: "office", Warnings: []string{}}
	got := extractPptx(officeOptions(input, "quality.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"), base)
	if !got.OK {
		t.Fatalf("extract pptx failed: %+v", got)
	}
	for _, expected := range []string{"[Slide 1]", "Quarterly Review", "[Notes]", "Emphasize year-over-year growth."} {
		if !strings.Contains(got.Text, expected) {
			t.Fatalf("pptx output missing %q:\n%s", expected, got.Text)
		}
	}
	if got.Slides != 1 {
		t.Fatalf("expected one slide, got %d", got.Slides)
	}
}

func buildPDF(objects []string) []byte {
	var output bytes.Buffer
	output.WriteString("%PDF-1.4\n")
	offsets := make([]int, len(objects)+1)
	for index, body := range objects {
		offsets[index+1] = output.Len()
		fmt.Fprintf(&output, "%d 0 obj\n%s\nendobj\n", index+1, body)
	}
	xrefOffset := output.Len()
	fmt.Fprintf(&output, "xref\n0 %d\n", len(objects)+1)
	output.WriteString("0000000000 65535 f \n")
	for index := 1; index <= len(objects); index++ {
		fmt.Fprintf(&output, "%010d 00000 n \n", offsets[index])
	}
	fmt.Fprintf(&output, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", len(objects)+1, xrefOffset)
	return output.Bytes()
}

func writePDFFixture(t *testing.T) string {
	t.Helper()
	pageOne := "BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET"
	data := buildPDF([]string{
		"<< /Type /Catalog /Pages 2 0 R >>",
		"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
		"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
		"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
		"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
		fmt.Sprintf("<< /Length %d >>\nstream\n%s\nendstream", len(pageOne), pageOne),
		"<< /Length 0 >>\nstream\n\nendstream",
	})
	fixturePath := filepath.Join(t.TempDir(), "quality.pdf")
	if err := os.WriteFile(fixturePath, data, 0o600); err != nil {
		t.Fatalf("write pdf fixture: %v", err)
	}
	return fixturePath
}

func pdfOptions(input string, maxPages int) options {
	return options{
		input:       input,
		filename:    "quality.pdf",
		contentType: "application/pdf",
		pdfMaxBytes: 1024 * 1024,
		pdfMaxChars: 128 * 1024,
		pdfMaxPages: maxPages,
	}
}

func TestExtractPDFPageWarnings(t *testing.T) {
	input := writePDFFixture(t)
	info, err := os.Stat(input)
	if err != nil {
		t.Fatalf("stat pdf fixture: %v", err)
	}
	base := result{ParserVersion: parserVersion, Format: "pdf", Category: "pdf", Warnings: []string{}}
	got := extractPDF(pdfOptions(input, 20), base, info.Size())
	if !got.OK {
		t.Fatalf("extract pdf failed: %+v", got)
	}
	if !strings.Contains(got.Text, "[Page 1]") || !strings.Contains(got.Text, "Hello PDF") {
		t.Fatalf("unexpected pdf text:\n%s", got.Text)
	}
	if !containsString(got.Warnings, "page_2_no_text") {
		t.Fatalf("expected page-level no-text warning, got %v", got.Warnings)
	}
}

func TestExtractPDFPageLimitWarning(t *testing.T) {
	input := writePDFFixture(t)
	info, err := os.Stat(input)
	if err != nil {
		t.Fatalf("stat pdf fixture: %v", err)
	}
	base := result{ParserVersion: parserVersion, Format: "pdf", Category: "pdf", Warnings: []string{}}
	got := extractPDF(pdfOptions(input, 1), base, info.Size())
	if !got.OK {
		t.Fatalf("extract pdf failed: %+v", got)
	}
	if !got.Truncated || !containsString(got.Warnings, "pages_2_to_2_skipped_by_limit") {
		t.Fatalf("expected page-limit warning and truncation, got %+v", got)
	}
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}
