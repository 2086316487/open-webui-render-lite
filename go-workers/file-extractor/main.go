package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"encoding/xml"
	"flag"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/ledongthuc/pdf"
)

const (
	defaultTextMaxBytes   = 128 * 1024
	defaultOfficeMaxBytes = 2 * 1024 * 1024
	defaultOfficeMaxChars = 128 * 1024
	defaultPDFMaxBytes    = 4 * 1024 * 1024
	defaultPDFMaxChars    = 128 * 1024
	defaultPDFMaxPages    = 20
	defaultMaxSheets      = 5
	defaultMaxRows        = 500
	maxOfficeXMLBytes     = 8 * 1024 * 1024
	maxCellsPerRow        = 50
)

type options struct {
	input          string
	filename       string
	contentType    string
	textMaxBytes   int64
	officeMaxBytes int64
	officeMaxChars int
	maxSheets      int
	maxRows        int
	pdfMaxBytes    int64
	pdfMaxChars    int
	pdfMaxPages    int
}

type result struct {
	OK            bool     `json:"ok"`
	Format        string   `json:"format"`
	Category      string   `json:"category"`
	Text          string   `json:"text"`
	Pages         int      `json:"pages"`
	Sheets        int      `json:"sheets"`
	Slides        int      `json:"slides"`
	Truncated     bool     `json:"truncated"`
	Warnings      []string `json:"warnings"`
	ErrorCode     string   `json:"error_code"`
	UserMessageZH string   `json:"user_message_zh"`
}

type lineLimiter struct {
	lines     []string
	chars     int
	maxChars  int
	truncated bool
}

func main() {
	if len(os.Args) >= 2 && os.Args[1] == "s3-serve" {
		runS3Server(os.Args[2:])
		return
	}

	if len(os.Args) >= 2 && os.Args[1] == "s3" {
		runS3(os.Args[2:])
		return
	}

	if len(os.Args) < 2 || os.Args[1] != "extract" {
		writeResult(fail("", "unsupported_command"))
		return
	}

	opts, err := parseExtractOptions(os.Args[2:])
	if err != nil {
		writeResult(fail("", err.Error()))
		return
	}

	res := extract(opts)
	writeResult(res)
}

func parseExtractOptions(args []string) (options, error) {
	fs := flag.NewFlagSet("extract", flag.ContinueOnError)
	fs.SetOutput(io.Discard)

	opts := options{}
	fs.StringVar(&opts.input, "input", "", "input file path")
	fs.StringVar(&opts.filename, "filename", "", "original filename")
	fs.StringVar(&opts.contentType, "content-type", "", "uploaded content type")
	fs.Int64Var(&opts.textMaxBytes, "text-max-bytes", defaultTextMaxBytes, "max text bytes")
	fs.Int64Var(&opts.officeMaxBytes, "office-max-bytes", defaultOfficeMaxBytes, "max office bytes")
	fs.IntVar(&opts.officeMaxChars, "office-max-chars", defaultOfficeMaxChars, "max office chars")
	fs.IntVar(&opts.maxSheets, "max-sheets", defaultMaxSheets, "max spreadsheet sheets")
	fs.IntVar(&opts.maxRows, "max-rows", defaultMaxRows, "max spreadsheet rows")
	fs.Int64Var(&opts.pdfMaxBytes, "pdf-max-bytes", defaultPDFMaxBytes, "max pdf bytes")
	fs.IntVar(&opts.pdfMaxChars, "pdf-max-chars", defaultPDFMaxChars, "max pdf chars")
	fs.IntVar(&opts.pdfMaxPages, "pdf-max-pages", defaultPDFMaxPages, "max pdf pages")

	if err := fs.Parse(args); err != nil {
		return opts, fmt.Errorf("invalid_arguments")
	}
	if strings.TrimSpace(opts.input) == "" {
		return opts, fmt.Errorf("missing_input")
	}
	if strings.TrimSpace(opts.filename) == "" {
		opts.filename = filepath.Base(opts.input)
	}
	return opts, nil
}

func extract(opts options) result {
	ext := fileExt(opts.filename)
	res := result{
		OK:       false,
		Format:   strings.TrimPrefix(ext, "."),
		Category: categoryFor(opts.filename, opts.contentType),
		Warnings: []string{},
	}

	info, err := os.Stat(opts.input)
	if err != nil {
		return withFormat(fail(ext, "read_failed"), res.Category)
	}
	if info.Size() <= 0 {
		return withFormat(fail(ext, "empty_upload"), res.Category)
	}

	switch res.Category {
	case "image":
		res.OK = true
		return res
	case "text":
		return extractText(opts, res)
	case "office":
		return extractOffice(opts, res, info.Size())
	case "pdf":
		return extractPDF(opts, res, info.Size())
	default:
		return withFormat(fail(ext, "unsupported_file_type"), res.Category)
	}
}

func extractText(opts options, base result) result {
	if opts.textMaxBytes > 0 {
		if info, err := os.Stat(opts.input); err == nil && info.Size() > opts.textMaxBytes {
			return withFormat(fail(fileExt(opts.filename), "too_large"), base.Category)
		}
	}
	data, err := os.ReadFile(opts.input)
	if err != nil {
		return withFormat(fail(fileExt(opts.filename), "read_failed"), base.Category)
	}
	if bytes.Contains(data, []byte{0}) {
		return withFormat(fail(fileExt(opts.filename), "binary"), base.Category)
	}

	text := decodeText(data)
	if strings.TrimSpace(text) == "" {
		return withFormat(fail(fileExt(opts.filename), "decode_failed"), base.Category)
	}
	base.OK = true
	base.Text = strings.TrimSpace(text)
	return base
}

func extractOffice(opts options, base result, size int64) result {
	if opts.officeMaxBytes > 0 && size > opts.officeMaxBytes {
		return withFormat(fail(fileExt(opts.filename), "too_large"), base.Category)
	}

	ext := fileExt(opts.filename)
	switch {
	case ext == ".docx" || isContentType(opts.contentType, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
		return extractDocx(opts, base)
	case ext == ".xlsx" || isContentType(opts.contentType, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
		return extractXlsx(opts, base)
	case ext == ".pptx" || isContentType(opts.contentType, "application/vnd.openxmlformats-officedocument.presentationml.presentation"):
		return extractPptx(opts, base)
	default:
		return withFormat(fail(ext, "unsupported_file_type"), base.Category)
	}
}

func extractDocx(opts options, base result) result {
	reader, err := zip.OpenReader(opts.input)
	if err != nil {
		return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
	}
	defer reader.Close()

	files := zipFileMap(reader.File)
	names := make([]string, 0)
	for name := range files {
		if name == "word/document.xml" ||
			strings.HasPrefix(name, "word/header") ||
			strings.HasPrefix(name, "word/footer") ||
			name == "word/footnotes.xml" ||
			name == "word/endnotes.xml" {
			names = append(names, name)
		}
	}
	sort.Slice(names, func(i, j int) bool {
		left, right := names[i], names[j]
		leftMain := left == "word/document.xml"
		rightMain := right == "word/document.xml"
		if leftMain != rightMain {
			return leftMain
		}
		return left < right
	})
	if _, ok := files["word/document.xml"]; !ok {
		return withFormat(fail(fileExt(opts.filename), "invalid_office_file"), base.Category)
	}

	limiter := newLimiter(opts.officeMaxChars)
	for _, name := range names {
		lines, err := xmlParagraphLines(files[name])
		if err != nil {
			return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
		}
		for _, line := range lines {
			if !limiter.append(line) {
				break
			}
		}
		if limiter.truncated {
			break
		}
	}

	return textResult(base, limiter.text(), limiter.truncated, "empty")
}

func extractPptx(opts options, base result) result {
	reader, err := zip.OpenReader(opts.input)
	if err != nil {
		return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
	}
	defer reader.Close()

	files := zipFileMap(reader.File)
	names := make([]string, 0)
	for name := range files {
		if strings.HasPrefix(name, "ppt/slides/slide") && strings.HasSuffix(name, ".xml") {
			names = append(names, name)
		}
	}
	sort.Slice(names, func(i, j int) bool {
		return officePartNumber(names[i], "ppt/slides/slide", ".xml") < officePartNumber(names[j], "ppt/slides/slide", ".xml")
	})
	if len(names) == 0 {
		return withFormat(fail(fileExt(opts.filename), "invalid_office_file"), base.Category)
	}

	limiter := newLimiter(opts.officeMaxChars)
	for i, name := range names {
		lines, err := xmlParagraphLines(files[name])
		if err != nil {
			return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
		}
		if len(lines) > 0 {
			base.Slides++
			if !limiter.append(fmt.Sprintf("[Slide %d]", i+1)) {
				break
			}
		}
		for _, line := range lines {
			if !limiter.append(line) {
				break
			}
		}
		if limiter.truncated {
			break
		}
	}

	return textResult(base, limiter.text(), limiter.truncated, "empty")
}

func extractXlsx(opts options, base result) result {
	reader, err := zip.OpenReader(opts.input)
	if err != nil {
		return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
	}
	defer reader.Close()

	files := zipFileMap(reader.File)
	sheetNames := xlsxWorkbookSheetNames(files["xl/workbook.xml"])
	sharedStrings, err := xlsxSharedStrings(files["xl/sharedStrings.xml"])
	if err != nil {
		return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
	}

	worksheetNames := make([]string, 0)
	for name := range files {
		if strings.HasPrefix(name, "xl/worksheets/sheet") && strings.HasSuffix(name, ".xml") {
			worksheetNames = append(worksheetNames, name)
		}
	}
	sort.Slice(worksheetNames, func(i, j int) bool {
		return officePartNumber(worksheetNames[i], "xl/worksheets/sheet", ".xml") < officePartNumber(worksheetNames[j], "xl/worksheets/sheet", ".xml")
	})
	if len(worksheetNames) == 0 {
		return withFormat(fail(fileExt(opts.filename), "invalid_office_file"), base.Category)
	}

	limiter := newLimiter(opts.officeMaxChars)
	for i, name := range worksheetNames {
		if opts.maxSheets > 0 && i >= opts.maxSheets {
			limiter.truncated = true
			break
		}

		displayName := fmt.Sprintf("Sheet%d", i+1)
		if i < len(sheetNames) && strings.TrimSpace(sheetNames[i]) != "" {
			displayName = sheetNames[i]
		}
		if !limiter.append(fmt.Sprintf("[Sheet: %s]", displayName)) {
			break
		}
		base.Sheets++

		if err := xlsxSheetLines(files[name], sharedStrings, opts.maxRows, limiter); err != nil {
			return withFormat(fail(fileExt(opts.filename), "office_extract_failed"), base.Category)
		}
		if limiter.truncated {
			break
		}
	}

	return textResult(base, limiter.text(), limiter.truncated, "empty")
}

func extractPDF(opts options, base result, size int64) result {
	if opts.pdfMaxBytes > 0 && size > opts.pdfMaxBytes {
		return withFormat(fail(fileExt(opts.filename), "too_large"), base.Category)
	}

	file, reader, err := pdf.Open(opts.input)
	if err != nil {
		if isEncryptedPDFError(err) {
			return withFormat(fail(fileExt(opts.filename), "encrypted"), base.Category)
		}
		return withFormat(fail(fileExt(opts.filename), "pdf_extract_failed"), base.Category)
	}
	defer file.Close()

	pageCount := reader.NumPage()
	base.Pages = pageCount
	if pageCount <= 0 {
		return withFormat(fail(fileExt(opts.filename), "empty"), base.Category)
	}

	maxPages := pageCount
	if opts.pdfMaxPages > 0 && maxPages > opts.pdfMaxPages {
		maxPages = opts.pdfMaxPages
	}

	limiter := newLimiter(opts.pdfMaxChars)
	fonts := map[string]*pdf.Font{}
	pageErrors := 0

	for pageIndex := 1; pageIndex <= maxPages; pageIndex++ {
		page := reader.Page(pageIndex)
		if page.V.IsNull() || page.V.Key("Contents").Kind() == pdf.Null {
			continue
		}

		pageText, err := page.GetPlainText(fonts)
		if err != nil {
			pageErrors++
			if len(base.Warnings) < 3 {
				base.Warnings = append(base.Warnings, fmt.Sprintf("page_%d_extract_failed", pageIndex))
			}
			continue
		}

		lines := normalizedLines(pageText)
		if len(lines) == 0 {
			continue
		}
		if !limiter.append(fmt.Sprintf("[Page %d]", pageIndex)) {
			break
		}
		for _, line := range lines {
			if !limiter.append(line) {
				break
			}
		}
		if limiter.truncated {
			break
		}
	}

	if pageCount > maxPages {
		limiter.truncated = true
	}
	text := limiter.text()
	if strings.TrimSpace(text) == "" && pageErrors > 0 {
		return withFormat(fail(fileExt(opts.filename), "pdf_extract_failed"), base.Category)
	}

	return textResult(base, text, limiter.truncated, "empty")
}

func isEncryptedPDFError(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "encrypted") || strings.Contains(msg, "password")
}

func zipFileMap(files []*zip.File) map[string]*zip.File {
	out := make(map[string]*zip.File, len(files))
	for _, file := range files {
		out[path.Clean(file.Name)] = file
	}
	return out
}

func readZipFile(file *zip.File) ([]byte, error) {
	if file == nil {
		return nil, nil
	}
	if file.UncompressedSize64 > maxOfficeXMLBytes {
		return nil, fmt.Errorf("too_large")
	}
	reader, err := file.Open()
	if err != nil {
		return nil, err
	}
	defer reader.Close()
	return io.ReadAll(io.LimitReader(reader, maxOfficeXMLBytes+1))
}

func xmlParagraphLines(file *zip.File) ([]string, error) {
	data, err := readZipFile(file)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return nil, nil
	}
	if len(data) > maxOfficeXMLBytes {
		return nil, fmt.Errorf("too_large")
	}

	decoder := xml.NewDecoder(bytes.NewReader(data))
	lines := []string{}
	inParagraph := false
	inText := false
	var paragraph strings.Builder
	var text strings.Builder

	flushText := func() {
		if text.Len() == 0 {
			return
		}
		paragraph.WriteString(text.String())
		text.Reset()
	}
	flushParagraph := func() {
		flushText()
		value := strings.TrimSpace(paragraph.String())
		paragraph.Reset()
		if value == "" {
			return
		}
		for _, line := range strings.Split(value, "\n") {
			line = strings.TrimSpace(line)
			if line != "" {
				lines = append(lines, line)
			}
		}
	}

	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		switch value := token.(type) {
		case xml.StartElement:
			switch value.Name.Local {
			case "p":
				inParagraph = true
				paragraph.Reset()
			case "t":
				if inParagraph {
					inText = true
					text.Reset()
				}
			case "tab":
				if inParagraph {
					flushText()
					paragraph.WriteRune('\t')
				}
			case "br", "cr":
				if inParagraph {
					flushText()
					paragraph.WriteRune('\n')
				}
			}
		case xml.CharData:
			if inParagraph && inText {
				text.Write([]byte(value))
			}
		case xml.EndElement:
			switch value.Name.Local {
			case "t":
				if inText {
					flushText()
					inText = false
				}
			case "p":
				if inParagraph {
					flushParagraph()
					inParagraph = false
					inText = false
				}
			}
		}
	}

	return lines, nil
}

func xlsxWorkbookSheetNames(file *zip.File) []string {
	data, err := readZipFile(file)
	if err != nil || len(data) == 0 {
		return nil
	}

	decoder := xml.NewDecoder(bytes.NewReader(data))
	names := []string{}
	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return names
		}
		start, ok := token.(xml.StartElement)
		if !ok || start.Name.Local != "sheet" {
			continue
		}
		for _, attr := range start.Attr {
			if attr.Name.Local == "name" && strings.TrimSpace(attr.Value) != "" {
				names = append(names, attr.Value)
				break
			}
		}
	}
	return names
}

func xlsxSharedStrings(file *zip.File) ([]string, error) {
	data, err := readZipFile(file)
	if err != nil || len(data) == 0 {
		return nil, err
	}

	decoder := xml.NewDecoder(bytes.NewReader(data))
	values := []string{}
	inSI := false
	inT := false
	var current strings.Builder
	var text strings.Builder
	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		switch value := token.(type) {
		case xml.StartElement:
			switch value.Name.Local {
			case "si":
				inSI = true
				current.Reset()
			case "t":
				if inSI {
					inT = true
					text.Reset()
				}
			}
		case xml.CharData:
			if inSI && inT {
				text.Write([]byte(value))
			}
		case xml.EndElement:
			switch value.Name.Local {
			case "t":
				if inT {
					current.WriteString(text.String())
					text.Reset()
					inT = false
				}
			case "si":
				values = append(values, strings.TrimSpace(current.String()))
				current.Reset()
				inSI = false
				inT = false
			}
		}
	}
	return values, nil
}

func xlsxSheetLines(file *zip.File, sharedStrings []string, maxRows int, limiter *lineLimiter) error {
	data, err := readZipFile(file)
	if err != nil {
		return err
	}
	if len(data) == 0 {
		return nil
	}

	decoder := xml.NewDecoder(bytes.NewReader(data))
	rowIndex := 0
	inRow := false
	inCell := false
	inV := false
	inT := false
	cellType := ""
	var cellRaw strings.Builder
	rowValues := []string{}

	finishCell := func() {
		raw := strings.TrimSpace(cellRaw.String())
		cellRaw.Reset()
		if raw == "" {
			return
		}
		value := raw
		if cellType == "s" {
			index, err := strconv.Atoi(raw)
			if err == nil && index >= 0 && index < len(sharedStrings) {
				value = sharedStrings[index]
			}
		}
		value = strings.TrimSpace(value)
		if value != "" {
			if len(rowValues) < maxCellsPerRow {
				rowValues = append(rowValues, value)
			} else if len(rowValues) == maxCellsPerRow {
				rowValues = append(rowValues, "[row truncated]")
			}
		}
	}

	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		switch value := token.(type) {
		case xml.StartElement:
			switch value.Name.Local {
			case "row":
				rowIndex++
				if maxRows > 0 && rowIndex > maxRows {
					limiter.append(fmt.Sprintf("[Sheet truncated after %d rows.]", maxRows))
					limiter.truncated = true
					return nil
				}
				inRow = true
				rowValues = rowValues[:0]
			case "c":
				if inRow {
					inCell = true
					cellType = ""
					cellRaw.Reset()
					for _, attr := range value.Attr {
						if attr.Name.Local == "t" {
							cellType = attr.Value
							break
						}
					}
				}
			case "v":
				if inCell {
					inV = true
				}
			case "t":
				if inCell {
					inT = true
				}
			}
		case xml.CharData:
			if inCell && (inV || inT) {
				cellRaw.Write([]byte(value))
			}
		case xml.EndElement:
			switch value.Name.Local {
			case "v":
				inV = false
			case "t":
				inT = false
			case "c":
				if inCell {
					finishCell()
					inCell = false
					inV = false
					inT = false
				}
			case "row":
				if len(rowValues) > 0 {
					if !limiter.append(strings.Join(rowValues, "\t")) {
						return nil
					}
				}
				inRow = false
				inCell = false
			}
		}
	}
	return nil
}

func newLimiter(maxChars int) *lineLimiter {
	if maxChars <= 0 {
		maxChars = defaultOfficeMaxChars
	}
	return &lineLimiter{maxChars: maxChars}
}

func (l *lineLimiter) append(line string) bool {
	line = normalizeLine(line)
	if line == "" {
		return true
	}

	separatorLen := 0
	if len(l.lines) > 0 {
		separatorLen = 1
	}
	remaining := l.maxChars - l.chars - separatorLen
	if remaining <= 0 {
		l.truncated = true
		return false
	}

	lineLen := utf8.RuneCountInString(line)
	if lineLen > remaining {
		l.lines = append(l.lines, takeRunes(line, remaining))
		l.chars = l.maxChars
		l.truncated = true
		return false
	}

	l.lines = append(l.lines, line)
	l.chars += lineLen + separatorLen
	return true
}

func (l *lineLimiter) text() string {
	text := strings.TrimSpace(strings.Join(l.lines, "\n"))
	if text != "" && l.truncated {
		text += "\n\n[Content truncated for Render lite context limit.]"
	}
	return text
}

func textResult(base result, text string, truncated bool, emptyCode string) result {
	if strings.TrimSpace(text) == "" {
		return withFormat(fail("."+base.Format, emptyCode), base.Category)
	}
	base.OK = true
	base.Text = strings.TrimSpace(text)
	base.Truncated = truncated
	return base
}

func normalizedLines(raw string) []string {
	raw = strings.ReplaceAll(raw, "\x00", "")
	lines := []string{}
	for _, line := range strings.Split(raw, "\n") {
		line = normalizeLine(line)
		if line != "" {
			lines = append(lines, line)
		}
	}
	return lines
}

func normalizeLine(value string) string {
	return strings.Join(strings.Fields(strings.ReplaceAll(value, "\x00", "")), " ")
}

func takeRunes(value string, count int) string {
	if count <= 0 {
		return ""
	}
	runes := []rune(value)
	if len(runes) <= count {
		return value
	}
	return string(runes[:count])
}

func decodeText(data []byte) string {
	if len(data) >= 3 && data[0] == 0xef && data[1] == 0xbb && data[2] == 0xbf {
		data = data[3:]
	}
	if utf8.Valid(data) {
		return string(data)
	}
	var builder strings.Builder
	for _, value := range data {
		builder.WriteRune(rune(value))
	}
	return builder.String()
}

func officePartNumber(name string, prefix string, suffix string) int {
	base := strings.TrimSuffix(strings.TrimPrefix(name, prefix), suffix)
	value, err := strconv.Atoi(base)
	if err != nil {
		return 0
	}
	return value
}

func categoryFor(filename string, contentType string) string {
	ext := fileExt(filename)
	contentType = strings.ToLower(strings.TrimSpace(contentType))
	switch {
	case contentType == "application/pdf" || contentType == "application/x-pdf" || ext == ".pdf":
		return "pdf"
	case strings.HasPrefix(contentType, "image/") || isImageExt(ext):
		return "image"
	case strings.HasPrefix(contentType, "text/") || isTextExt(ext) || isTextContentType(contentType):
		return "text"
	case ext == ".docx" || ext == ".xlsx" || ext == ".pptx" || isOfficeContentType(contentType):
		return "office"
	default:
		return "unsupported"
	}
}

func fileExt(filename string) string {
	return strings.ToLower(filepath.Ext(filename))
}

func isContentType(actual string, expected string) bool {
	return strings.EqualFold(strings.TrimSpace(actual), expected)
}

func isOfficeContentType(contentType string) bool {
	switch contentType {
	case "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
		"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		"application/vnd.openxmlformats-officedocument.presentationml.presentation":
		return true
	default:
		return false
	}
}

func isTextContentType(contentType string) bool {
	switch contentType {
	case "application/json",
		"application/jsonl",
		"application/javascript",
		"application/x-javascript",
		"application/xml",
		"application/yaml",
		"application/x-yaml":
		return true
	default:
		return false
	}
}

func isTextExt(ext string) bool {
	switch ext {
	case ".bat", ".c", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv", ".go", ".h", ".hpp",
		".html", ".ini", ".java", ".js", ".json", ".jsonl", ".jsx", ".log", ".md", ".php",
		".ps1", ".py", ".rb", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsv", ".tsx",
		".txt", ".xml", ".yaml", ".yml":
		return true
	default:
		return false
	}
}

func isImageExt(ext string) bool {
	switch ext {
	case ".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".tiff", ".tif", ".ico", ".heic", ".heif", ".avif":
		return true
	default:
		return false
	}
}

func fail(ext string, code string) result {
	format := strings.TrimPrefix(ext, ".")
	return result{
		OK:            false,
		Format:        format,
		Category:      categoryFor("file"+ext, ""),
		Text:          "",
		Warnings:      []string{},
		ErrorCode:     code,
		UserMessageZH: messageZH(format, code),
	}
}

func withFormat(res result, category string) result {
	if category != "" {
		res.Category = category
	}
	if res.UserMessageZH == "" {
		res.UserMessageZH = messageZH(res.Format, res.ErrorCode)
	}
	return res
}

func messageZH(format string, code string) string {
	ext := "." + strings.TrimPrefix(format, ".")
	switch code {
	case "empty_upload":
		return "不能上传空文件。请选择有内容的文件后再上传。"
	case "too_large":
		return "文件超过 Render lite 的轻量处理限制。请缩小文件，或使用 Hugging Face full。"
	case "encrypted":
		return "这个文件似乎已加密，Render lite 无法读取。请解除密码后再上传。"
	case "empty":
		if ext == ".pdf" {
			return "这个 PDF 没有检测到可复制文字，可能是扫描件。可以尝试转为图片后让视觉模型读取。"
		}
		return "这个文件没有提取到可读取文字。请确认文件内容不是图片、加密内容或损坏文件。"
	case "binary", "decode_failed":
		return "这个文件没有提取到可读取文字。请确认文件内容不是图片、加密内容或损坏文件。"
	case "invalid_office_file", "office_extract_failed":
		return "这个 Office 文件无法读取。请重新另存为新版 Office 格式后再上传。"
	case "pdf_extract_failed":
		return "这个 PDF 无法读取。请确认文件未损坏，或使用 Hugging Face full。"
	case "unsupported_file_type":
		switch ext {
		case ".doc":
			return "当前 Render lite 不支持 .doc 老版 Word 文件。请在本地另存为 .docx 后再上传。"
		case ".xls":
			return "当前 Render lite 不支持 .xls 老版 Excel 文件。请在本地另存为 .xlsx 后再上传。"
		case ".ppt":
			return "当前 Render lite 不支持 .ppt 老版 PowerPoint 文件。请在本地另存为 .pptx 后再上传。"
		default:
			return "当前 Render lite 不支持上传这种文件格式。请转换为支持的格式后再上传。"
		}
	default:
		return "文件上传失败：Render lite 没能读取这个文件的内容。请转换格式后重试，或使用 Hugging Face full。"
	}
}

func writeResult(res result) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(res); err != nil {
		fmt.Fprintf(os.Stdout, `{"ok":false,"error_code":"json_encode_failed","user_message_zh":"文件上传失败，请稍后重试。"}`+"\n")
	}
}
