package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode"

	"github.com/gin-gonic/gin"
	"github.com/neurosnap/sentences"
	"golang.org/x/net/html"
)

// Estructura para la petición JSON de entrada
type PreprocessingRequest struct {
	Text string `json:"text"`
}

// Estructura para la respuesta JSON de salida
type PreprocessingResponse struct {
	Chunks   []string              `json:"chunks"`
	Entities map[int][]EntityMatch `json:"entities"`
}

// Estructura para una entidad encontrada
type EntityMatch struct {
	Text  string `json:"text"`
	Label string `json:"label"`
}

// Petición para validar entidades estructuradas
type ValidationRequest struct {
	Entities []EntityMatch `json:"entities"`
}

// Respuesta de validación de entidades estructuradas
type ValidationResponse struct {
	Valid   []EntityMatch `json:"valid"`
	Invalid []EntityMatch `json:"invalid,omitempty"`
}

// Estructura para leer las reglas de regex del fichero JSON
type RegexRule struct {
	Label   string `json:"label"`
	Pattern string `json:"pattern"`
}

// Almacenamos las reglas de regex ya compiladas para máxima eficiencia
var compiledRules []struct {
	Label string
	Regex *regexp.Regexp
}

var labelAlias = map[string]string{
	"PHONE_INTL": "PHONE",
	"BTC":        "CRYPTO_BTC_ADDRESS",
	"BITCOIN":    "CRYPTO_BTC_ADDRESS",
	"BIC":        "SWIFT_BIC",
	"SWIFT":      "SWIFT_BIC",
	"GEO_DD":     "GEOLOCATION_DD",
	"GEO_DMS":    "GEOLOCATION_DMS",
}

var defangReplacements = []struct {
	old string
	new string
}{
	{"[.]", "."},
	{"(dot)", "."},
	{"[dot]", "."},
	{" dot ", "."},
	{" (at) ", "@"},
	{"[at]", "@"},
	{"{at}", "@"},
	{" at ", "@"},
	{"arroba", "@"},
	{"hxxp://", "http://"},
	{"hxxps://", "https://"},
}

var allowedLicenseLetters = map[rune]bool{
	'B': true, 'C': true, 'D': true, 'F': true, 'G': true, 'H': true,
	'J': true, 'K': true, 'L': true, 'M': true, 'N': true, 'P': true,
	'Q': true, 'R': true, 'S': true, 'T': true, 'V': true, 'W': true,
	'X': true, 'Y': true, 'Z': true,
}

const base58Alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

func normalizeDefanged(value string) string {
	out := value
	for _, repl := range defangReplacements {
		out = strings.ReplaceAll(out, repl.old, repl.new)
	}
	return out
}

func luhnCheck(number string) bool {
	digitsOnly := make([]rune, 0, len(number))
	for _, r := range number {
		if unicode.IsDigit(r) {
			digitsOnly = append(digitsOnly, r)
		}
	}
	if len(digitsOnly) < 12 {
		return false
	}
	sum := 0
	double := false
	for i := len(digitsOnly) - 1; i >= 0; i-- {
		n := int(digitsOnly[i] - '0')
		if double {
			n *= 2
			if n > 9 {
				n -= 9
			}
		}
		sum += n
		double = !double
	}
	return sum%10 == 0
}

func dniESCheck(value string) bool {
	trimmed := strings.ToUpper(strings.TrimSpace(value))
	if len(trimmed) != 9 {
		return false
	}
	prefix := trimmed[:8]
	letter := trimmed[8]
	if _, err := strconv.Atoi(prefix); err != nil {
		return false
	}
	letters := "TRWAGMYFPDXBNJZSQVHLCKE"
	idx, _ := strconv.Atoi(prefix)
	return letters[idx%23] == letter
}

func ibanMod97(iban string) bool {
	if len(iban) < 4 {
		return false
	}
	rearranged := iban[4:] + iban[:4]
	converted := strings.Builder{}
	for _, r := range rearranged {
		if unicode.IsDigit(r) {
			converted.WriteRune(r)
		} else if unicode.IsLetter(r) {
			upper := unicode.ToUpper(r)
			converted.WriteString(strconv.Itoa(int(upper - 'A' + 10)))
		} else {
			return false
		}
	}
	remainder := 0
	for _, r := range converted.String() {
		remainder = (remainder*10 + int(r-'0')) % 97
	}
	return remainder == 1
}

func isBase58(candidate string) bool {
	if len(candidate) == 0 {
		return false
	}
	for _, r := range candidate {
		if !strings.ContainsRune(base58Alphabet, r) {
			return false
		}
	}
	return true
}

func parseDMSCoordinate(segment string) (float64, bool) {
	if segment == "" {
		return 0, false
	}
	trimmed := strings.TrimSpace(segment)
	upper := strings.ToUpper(trimmed)
	orientation := byte(0)
	for i := len(upper) - 1; i >= 0; i-- {
		ch := upper[i]
		if ch == 'N' || ch == 'S' || ch == 'E' || ch == 'W' || ch == 'O' {
			orientation = upper[i]
			break
		}
	}
	if orientation == 0 {
		return 0, false
	}

	validSeparators := map[rune]bool{
		'°': true, 'º': true, 'D': true, '\'': true, '´': true, '’': true,
		'M': true, '"': true, '”': true, 'S': true, ' ': true,
	}
	digits := []string{}
	current := strings.Builder{}
	for _, r := range trimmed {
		if unicode.IsDigit(r) || r == '.' {
			current.WriteRune(r)
			continue
		}
		if validSeparators[unicode.ToUpper(r)] {
			if current.Len() > 0 {
				digits = append(digits, current.String())
				current.Reset()
			}
		} else {
			if current.Len() > 0 {
				digits = append(digits, current.String())
				current.Reset()
			}
		}
	}
	if current.Len() > 0 {
		digits = append(digits, current.String())
	}
	if len(digits) < 3 {
		return 0, false
	}
	degrees, err1 := strconv.ParseFloat(digits[0], 64)
	minutes, err2 := strconv.ParseFloat(digits[1], 64)
	seconds, err3 := strconv.ParseFloat(digits[2], 64)
	if err1 != nil || err2 != nil || err3 != nil {
		return 0, false
	}
	if minutes < 0 || minutes >= 60 || seconds < 0 || seconds >= 60 {
		return 0, false
	}
	value := degrees + minutes/60 + seconds/3600
	if orientation == 'S' || orientation == 'W' || orientation == 'O' {
		value *= -1
	}
	return value, true
}

func splitDMSPairs(value string) []string {
	var segments []string
	upper := strings.ToUpper(value)
	start := 0
	for idx := range upper {
		ch := upper[idx]
		if ch == 'N' || ch == 'S' || ch == 'E' || ch == 'W' || ch == 'O' {
			segment := strings.TrimSpace(value[start : idx+1])
			if segment != "" {
				segments = append(segments, segment)
			}
			start = idx + 1
		}
	}
	if start < len(value) {
		tail := strings.TrimSpace(value[start:])
		if tail != "" {
			segments = append(segments, tail)
		}
	}
	filtered := make([]string, 0, len(segments))
	for _, seg := range segments {
		if seg != "" {
			filtered = append(filtered, seg)
		}
	}
	return filtered
}

func isValidDate(year, month, day int) bool {
	// Verifica si una fecha es válida considerando días por mes y años bisiestos
	if month < 1 || month > 12 {
		return false
	}

	daysInMonth := []int{31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31}

	// Ajustar febrero para años bisiestos
	if month == 2 && ((year%4 == 0 && year%100 != 0) || (year%400 == 0)) {
		daysInMonth[1] = 29
	}

	return day >= 1 && day <= daysInMonth[month-1]
}

func normalizeDate(dateStr string) string {
	// Normaliza fechas a formato DD/MM/YYYY
	dateStr = strings.TrimSpace(dateStr)

	// Mapas de meses
	spanishMonths := map[string]string{
		"enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
		"mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
		"septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
	}
	englishMonths := map[string]string{
		"january": "01", "february": "02", "march": "03", "april": "04",
		"may": "05", "june": "06", "july": "07", "august": "08",
		"september": "09", "october": "10", "november": "11", "december": "12",
	}

	// Intentar DD/MM/YYYY o D/M/YYYY
	re1 := regexp.MustCompile(`^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$`)
	if match := re1.FindStringSubmatch(dateStr); match != nil {
		day, _ := strconv.Atoi(match[1])
		month, _ := strconv.Atoi(match[2])
		year, _ := strconv.Atoi(match[3])

		// Validar rangos básicos
		if year < 1900 || year > 2100 || month < 1 || month > 12 || day < 1 || day > 31 {
			return ""
		}

		// Validar día según mes (incluyendo años bisiestos)
		if !isValidDate(year, month, day) {
			return ""
		}

		return fmt.Sprintf("%02d/%02d/%04d", day, month, year)
	}

	// Intentar YYYY-MM-DD o YYYY/MM/DD
	re2 := regexp.MustCompile(`^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$`)
	if match := re2.FindStringSubmatch(dateStr); match != nil {
		year, _ := strconv.Atoi(match[1])
		month, _ := strconv.Atoi(match[2])
		day, _ := strconv.Atoi(match[3])

		// Validar rangos básicos
		if year < 1900 || year > 2100 || month < 1 || month > 12 || day < 1 || day > 31 {
			return ""
		}

		// Validar día según mes (incluyendo años bisiestos)
		if !isValidDate(year, month, day) {
			return ""
		}

		return fmt.Sprintf("%02d/%02d/%04d", day, month, year)
	}

	// Intentar "8 de marzo de 2023" o "8 marzo 2023"
	re3 := regexp.MustCompile(`(?i)^(\d{1,2})\s+(?:de\s+)?(\w+)\s+(?:de\s+)?(\d{4})$`)
	if match := re3.FindStringSubmatch(dateStr); match != nil {
		day, _ := strconv.Atoi(match[1])
		monthName := strings.ToLower(match[2])
		year := match[3]

		if month, ok := spanishMonths[monthName]; ok {
			return fmt.Sprintf("%02d/%s/%s", day, month, year)
		}
		if month, ok := englishMonths[monthName]; ok {
			return fmt.Sprintf("%02d/%s/%s", day, month, year)
		}
	}

	// Intentar "March 8, 2023" o "March 8 2023"
	re4 := regexp.MustCompile(`(?i)^(\w+)\s+(\d{1,2}),?\s+(\d{4})$`)
	if match := re4.FindStringSubmatch(dateStr); match != nil {
		monthName := strings.ToLower(match[1])
		day, _ := strconv.Atoi(match[2])
		year, _ := strconv.Atoi(match[3])

		if year < 1900 || year > 2100 || day < 1 || day > 31 {
			return ""
		}

		if month, ok := englishMonths[monthName]; ok {
			return fmt.Sprintf("%02d/%s/%04d", day, month, year)
		}
	}

	// Intentar "enero 2023", "January 2023" (mes + año, sin día)
	re5 := regexp.MustCompile(`(?i)^(\w+)\s+(?:de\s+)?(\d{4})$`)
	if match := re5.FindStringSubmatch(dateStr); match != nil {
		monthName := strings.ToLower(match[1])
		year, _ := strconv.Atoi(match[2])

		if year < 1900 || year > 2100 {
			return ""
		}

		// Intentar español primero
		if month, ok := spanishMonths[monthName]; ok {
			return fmt.Sprintf("01/%s/%04d", month, year)
		}
		// Luego inglés
		if month, ok := englishMonths[monthName]; ok {
			return fmt.Sprintf("01/%s/%04d", month, year)
		}
	}

	// Si no se puede normalizar, devolver vacío
	return ""
}

func validateStructuredEntity(label, text string) bool {
	normalizedLabel := strings.ToUpper(strings.TrimSpace(label))
	raw := strings.TrimSpace(text)
	if normalizedLabel == "" || raw == "" {
		return false
	}

	if alias, ok := labelAlias[normalizedLabel]; ok {
		normalizedLabel = alias
	}

	norm := normalizeDefanged(raw)

	switch normalizedLabel {
	case "EMAIL":
		if strings.Contains(norm, " ") || !strings.Contains(norm, "@") {
			return false
		}
		parts := strings.SplitN(norm, "@", 2)
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return false
		}
		if !strings.Contains(parts[1], ".") {
			return false
		}
		if strings.HasPrefix(parts[1], ".") || strings.HasSuffix(parts[1], ".") {
			return false
		}
		return true
	case "PHONE":
		allowed := "0123456789+ -()"
		for _, r := range raw {
			if !strings.ContainsRune(allowed, r) {
				return false
			}
		}
		count := 0
		for _, r := range raw {
			if unicode.IsDigit(r) {
				count++
			}
		}
		return count >= 7 && count <= 15
	case "URL":
		parsed, err := url.Parse(norm)
		if err != nil {
			return false
		}
		if parsed.Scheme != "http" && parsed.Scheme != "https" {
			return false
		}
		if parsed.Host == "" {
			return false
		}
		return true
	case "CREDIT_CARD":
		digits := strings.Builder{}
		for _, r := range raw {
			if unicode.IsDigit(r) {
				digits.WriteRune(r)
			}
		}
		data := digits.String()
		if len(data) < 13 || len(data) > 19 {
			return false
		}
		return luhnCheck(data)
	case "IBAN_ES":
		iban := strings.ToUpper(strings.ReplaceAll(norm, " ", ""))
		if !strings.HasPrefix(iban, "ES") || len(iban) != 24 {
			return false
		}
		if _, err := strconv.Atoi(iban[2:]); err != nil {
			return false
		}
		return ibanMod97(iban)
	case "IBAN_INTL":
		iban := strings.ToUpper(strings.ReplaceAll(norm, " ", ""))
		if len(iban) < 15 || len(iban) > 34 {
			return false
		}
		// First two chars must be letters (country code)
		if iban[0] < 'A' || iban[0] > 'Z' || iban[1] < 'A' || iban[1] > 'Z' {
			return false
		}
		// Next two must be digits (check digits)
		if iban[2] < '0' || iban[2] > '9' || iban[3] < '0' || iban[3] > '9' {
			return false
		}
		// Rest must be alphanumeric
		for i := 4; i < len(iban); i++ {
			if !unicode.IsLetter(rune(iban[i])) && !unicode.IsDigit(rune(iban[i])) {
				return false
			}
		}
		return ibanMod97(iban)
	case "DNI_ES":
		return dniESCheck(raw)
	case "NIF_ES":
		value := strings.ToUpper(strings.ReplaceAll(raw, " ", ""))
		if len(value) != 9 {
			return false
		}
		initial := value[0]
		digits := value[1:8]
		control := value[8]
		var numeric string
		switch initial {
		case 'X', 'Y', 'Z':
			translation := map[byte]string{'X': "0", 'Y': "1", 'Z': "2"}
			numeric = translation[initial] + digits
		default:
			if initial < '0' || initial > '9' {
				return false
			}
			numeric = string(initial) + digits
		}
		if _, err := strconv.Atoi(numeric); err != nil {
			return false
		}
		if control < 'A' || control > 'Z' {
			return false
		}
		letters := "TRWAGMYFPDXBNJZSQVHLCKE"
		num, _ := strconv.Atoi(numeric)
		return letters[num%23] == control
	case "CIF_ES":
		value := strings.ToUpper(strings.ReplaceAll(raw, " ", ""))
		if len(value) != 9 {
			return false
		}
		if !strings.ContainsRune("ABCDEFGHJNPQRSUVW", rune(value[0])) {
			return false
		}
		if _, err := strconv.Atoi(value[1:8]); err != nil {
			return false
		}
		last := value[8]
		if last >= '0' && last <= '9' {
			return true
		}
		return strings.ContainsRune("ABCDEFGHJ", rune(last))
	case "VAT_ES":
		value := strings.ToUpper(strings.ReplaceAll(raw, " ", ""))
		if !strings.HasPrefix(value, "ES") {
			return false
		}
		suffix := value[2:]
		if len(suffix) < 9 || len(suffix) > 12 {
			return false
		}
		for _, r := range suffix {
			if !unicode.IsLetter(r) && !unicode.IsDigit(r) {
				return false
			}
		}
		return true
	case "CRYPTO_BTC_ADDRESS":
		candidate := strings.TrimSpace(raw)
		if len(candidate) < 26 || len(candidate) > 35 {
			return false
		}
		if candidate[0] != '1' && candidate[0] != '3' {
			return false
		}
		return isBase58(candidate)
	case "SWIFT_BIC":
		value := strings.ToUpper(strings.ReplaceAll(raw, " ", ""))
		if len(value) != 8 && len(value) != 11 {
			return false
		}
		for i := 0; i < 4; i++ {
			if value[i] < 'A' || value[i] > 'Z' {
				return false
			}
		}
		for i := 4; i < 6; i++ {
			if value[i] < 'A' || value[i] > 'Z' {
				return false
			}
		}
		for i := 6; i < 8; i++ {
			if !unicode.IsLetter(rune(value[i])) && !unicode.IsDigit(rune(value[i])) {
				return false
			}
		}
		if len(value) == 11 {
			for i := 8; i < 11; i++ {
				if !unicode.IsLetter(rune(value[i])) && !unicode.IsDigit(rune(value[i])) {
					return false
				}
			}
		}
		return true
	case "LICENSE_PLATE_ES":
		value := strings.ToUpper(strings.ReplaceAll(raw, " ", ""))
		if len(value) != 7 {
			return false
		}
		if _, err := strconv.Atoi(value[:4]); err != nil {
			return false
		}
		for _, r := range value[4:] {
			if !allowedLicenseLetters[r] {
				return false
			}
		}
		return true
	case "GEOLOCATION_DD":
		sanitized := strings.ReplaceAll(norm, ",", " ")
		sanitized = strings.ReplaceAll(sanitized, "°", " ")
		parts := strings.Fields(sanitized)
		if len(parts) < 2 {
			return false
		}
		lat, err1 := strconv.ParseFloat(parts[0], 64)
		lon, err2 := strconv.ParseFloat(parts[1], 64)
		if err1 != nil || err2 != nil {
			return false
		}
		return lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
	case "GEOLOCATION_DMS":
		segments := splitDMSPairs(norm)
		if len(segments) < 2 {
			return false
		}
		lat, okLat := parseDMSCoordinate(segments[0])
		lon, okLon := parseDMSCoordinate(segments[1])
		if !okLat || !okLon {
			return false
		}
		return lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
	default:
		return false
	}
}

// --- LÓGICA DE LIMPIEZA Y TROCEADO ---

// extrae texto de un fragmento de HTML, una versión más simple que la de soup.
func extractTextFromHTML(htmlString string) string {
	doc, err := html.Parse(strings.NewReader(htmlString))
	if err != nil {
		return htmlString // Si falla el parseo, devolvemos el texto original
	}
	var b strings.Builder
	var f func(*html.Node)
	f = func(n *html.Node) {
		if n.Type == html.TextNode {
			b.WriteString(n.Data)
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			f(c)
		}
	}
	f(doc)
	return b.String()
}

// divide el texto en frases y luego las agrupa en chunks.
func textToChunks(text string, chunkSize int) []string {
	// Inicializar el tokenizer con un modelo de almacenamiento válido
	storage := sentences.NewStorage()
	tokenizer := sentences.NewSentenceTokenizer(storage)

	// Tokenizar el texto en frases
	sentences := tokenizer.Tokenize(text)

	var chunks []string
	var currentChunk string

	for _, sentence := range sentences {
		if len(currentChunk)+len(sentence.Text) > chunkSize {
			if currentChunk != "" {
				chunks = append(chunks, strings.TrimSpace(currentChunk))
			}
			currentChunk = sentence.Text
		} else {
			currentChunk += " " + sentence.Text
		}
	}
	if currentChunk != "" {
		chunks = append(chunks, strings.TrimSpace(currentChunk))
	}
	return chunks
}

// --- HANDLER PRINCIPAL ---

func preprocessHandler(c *gin.Context) {
	var req PreprocessingRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Cuerpo de la petición inválido"})
		return
	}

	// 1. Limpiar HTML y normalizar espacios
	cleanText := extractTextFromHTML(req.Text)
	cleanText = regexp.MustCompile(`\s+`).ReplaceAllString(cleanText, " ")

	// 2. Trocear en chunks semánticos (basado en frases)
	chunks := textToChunks(cleanText, 1000)

	// 3. Extraer entidades basadas en regex para cada chunk
	entitiesByChunk := make(map[int][]EntityMatch)
	for i, chunk := range chunks {
		var matches []EntityMatch
		for _, rule := range compiledRules {
			found := rule.Regex.FindAllString(chunk, -1)
			if len(found) > 0 {
				for _, matchText := range found {
					// Para fechas, normalizar antes de validar
					if strings.ToUpper(rule.Label) == "DATE" {
						normalized := normalizeDate(matchText)
						if normalized != "" {
							matches = append(matches, EntityMatch{Text: normalized, Label: "DATE"})
						}
					} else if validateStructuredEntity(rule.Label, matchText) {
						matches = append(matches, EntityMatch{Text: strings.TrimSpace(matchText), Label: rule.Label})
					}
				}
			}
		}
		if len(matches) > 0 {
			entitiesByChunk[i] = matches
		}
	}

	log.Printf("INFO: Texto procesado en %d chunks.", len(chunks))
	c.JSON(http.StatusOK, PreprocessingResponse{Chunks: chunks, Entities: entitiesByChunk})
}

func validateHandler(c *gin.Context) {
	var req ValidationRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Cuerpo de la petición inválido"})
		return
	}

	valid := make([]EntityMatch, 0, len(req.Entities))
	invalid := make([]EntityMatch, 0)

	for _, entity := range req.Entities {
		label := strings.ToUpper(strings.TrimSpace(entity.Label))
		text := strings.TrimSpace(entity.Text)
		if label == "" || text == "" {
			invalid = append(invalid, EntityMatch{Text: text, Label: label})
			continue
		}
		if validateStructuredEntity(label, text) {
			valid = append(valid, EntityMatch{Text: text, Label: label})
		} else {
			invalid = append(invalid, EntityMatch{Text: text, Label: label})
		}
	}

	resp := ValidationResponse{Valid: valid}
	if len(invalid) > 0 {
		resp.Invalid = invalid
	}
	c.JSON(http.StatusOK, resp)
}

// --- INICIALIZACIÓN ---

func init() {
	log.Println("INFO: Cargando y compilando reglas de regex desde regex_patterns.json...")

	file, err := os.ReadFile("regex_patterns.json")
	if err != nil {
		log.Fatalf("FATAL: No se pudo leer el fichero de reglas: %v", err)
	}

	var rules []RegexRule
	if err := json.Unmarshal(file, &rules); err != nil {
		log.Fatalf("FATAL: No se pudo parsear el JSON de reglas: %v", err)
	}

	for _, rule := range rules {
		compiled, err := regexp.Compile(rule.Pattern)
		if err != nil {
			log.Printf("ADVERTENCIA: No se pudo compilar la regex para '%s'. Saltando. Error: %v", rule.Label, err)
			continue
		}
		compiledRules = append(compiledRules, struct {
			Label string
			Regex *regexp.Regexp
		}{Label: rule.Label, Regex: compiled})
	}
	log.Printf("INFO: %d reglas de regex cargadas y compiladas.", len(compiledRules))
}

// --- FUNCIÓN MAIN ---

func main() {
	gin.SetMode(gin.ReleaseMode)
	router := gin.Default()
	router.POST("/preprocess", preprocessHandler)
	router.POST("/validate", validateHandler)
	healthHandler := func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "healthy"})
	}
	router.GET("/health", healthHandler)
	router.HEAD("/health", healthHandler)
	log.Println("INFO: Servicio de pre-procesamiento escuchando en el puerto 8081...")

	srv := &http.Server{
		Addr:         ":8081",
		Handler:      router,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("FATAL: El servidor falló al arrancar: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("INFO: Apagando regex-entity-extractor...")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := srv.Shutdown(ctx); err != nil {
		log.Fatalf("FATAL: El servidor fue forzado a apagarse: %v", err)
	}
	log.Println("INFO: Servidor apagado limpiamente.")
}
