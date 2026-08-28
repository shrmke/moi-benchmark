package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	_ "github.com/go-sql-driver/mysql"
	mysqlDriver "github.com/go-sql-driver/mysql"
	"github.com/matrixflow/moi-core/agent-tools/knowledge"
	knowledgeservice "github.com/matrixflow/moi-core/agent-tools/knowledge/service"
	"github.com/matrixflow/moi-core/workers/go-worker/pkg/workitems"
)

const (
	schemaVersion                 = "matrixflow-product-rag-local-v2"
	taasAPIBaseURL                = "https://token.moi.matrixorigin.cn/v1"
	taasAPIKeyEnvName             = "TAAS_API_KEY"
	maasAPIBaseURL                = "https://api.modelarts-maas.com/v1"
	maasAPIKeyEnvName             = "MAAS_API_KEY"
	maxAPIResponseBytes           = 64 << 20
	defaultGenerationContextBytes = 100_000
)

var identifierPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

type Config struct {
	MatrixOne                     MatrixOneConfig  `json:"matrixone"`
	Workspace                     string           `json:"workspace_id"`
	MatrixFlowRoot                string           `json:"matrixflow_root,omitempty"`
	ChunkSize                     int              `json:"chunk_size"`
	Overlap                       int              `json:"chunk_overlap"`
	SectionSize                   int              `json:"section_size"`
	EmbeddingBatchSize            int              `json:"embedding_batch_size,omitempty"`
	EmbeddingConcurrency          int              `json:"embedding_concurrency,omitempty"`
	InsertBatchSize               int              `json:"insert_batch_size,omitempty"`
	SkipVectorIndexRebuild        bool             `json:"skip_vector_index_rebuild,omitempty"`
	SkipFullTextIndexDuringIngest bool             `json:"skip_fulltext_index_during_ingest,omitempty"`
	LikeFullTextFallback          bool             `json:"like_fulltext_fallback,omitempty"`
	Embedding                     EndpointConfig   `json:"embedding"`
	Generation                    GenerationConfig `json:"generation"`
}

type MatrixOneConfig struct {
	DSN                        string `json:"dsn"`
	Database                   string `json:"database"`
	VectorTable                string `json:"vector_table"`
	SkipExhaustiveFileIDFilter bool   `json:"skip_exhaustive_file_id_filter,omitempty"`
}

type EndpointConfig struct {
	Mode           string `json:"mode"`
	BaseURL        string `json:"base_url"`
	Model          string `json:"model"`
	APIKeyEnv      string `json:"api_key_env"`
	Dimension      int    `json:"dimension"`
	TimeoutSeconds int    `json:"timeout_seconds"`
	// Embedding retry settings. Generation has its own bounded retry/fallback
	// policy below so a single provider outage does not discard the whole QA run.
	RetryMaxAttempts    int     `json:"retry_max_attempts,omitempty"`
	RetryBackoffSeconds float64 `json:"retry_backoff_seconds,omitempty"`
}

type GenerationConfig struct {
	Enabled             bool                      `json:"enabled"`
	Provider            string                    `json:"provider"`
	BaseURL             string                    `json:"base_url"`
	Model               string                    `json:"model"`
	APIKeyEnv           string                    `json:"api_key_env"`
	TimeoutSeconds      int                       `json:"timeout_seconds"`
	RetryMaxAttempts    int                       `json:"retry_max_attempts,omitempty"`
	RetryBackoffSeconds float64                   `json:"retry_backoff_seconds,omitempty"`
	Thinking            string                    `json:"thinking,omitempty"`
	IncludePageImages   *bool                     `json:"include_page_images,omitempty"`
	MaxContextBytes     int                       `json:"max_context_bytes,omitempty"`
	SystemPrompt        string                    `json:"system_prompt,omitempty"`
	Fallback            *GenerationFallbackConfig `json:"fallback,omitempty"`
	// MMDocIR can route text-only and visual questions to different models
	// while keeping the shared embedding/retrieval contract unchanged.
	Text       *GenerationConfig `json:"text,omitempty"`
	Multimodal *GenerationConfig `json:"multimodal,omitempty"`
}

type GenerationFallbackConfig struct {
	Enabled             bool    `json:"enabled"`
	Provider            string  `json:"provider"`
	BaseURL             string  `json:"base_url"`
	Model               string  `json:"model"`
	APIKeyEnv           string  `json:"api_key_env"`
	TimeoutSeconds      int     `json:"timeout_seconds"`
	RetryMaxAttempts    int     `json:"retry_max_attempts,omitempty"`
	RetryBackoffSeconds float64 `json:"retry_backoff_seconds,omitempty"`
}

type SourceDocument struct {
	FileID string `json:"file_id"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Text   string `json:"-"`
}

type IndexedChunk struct {
	ID         string         `json:"id"`
	FileID     string         `json:"file_id"`
	FileName   string         `json:"file_name"`
	PageNumber int            `json:"page_number,omitempty"`
	Index      int            `json:"chunk_index"`
	Start      int            `json:"start_offset"`
	End        int            `json:"end_offset"`
	Content    string         `json:"content"`
	Embedding  []float64      `json:"-"`
	Metadata   map[string]any `json:"-"`
	IndexVer   int64          `json:"index_version"`
	ContentSHA string         `json:"sha256"`
	Level      string         `json:"level"`
	DocID      string         `json:"doc_id,omitempty"`
	SectionID  string         `json:"section_id,omitempty"`
}

type IngestState struct {
	SchemaVersion  string           `json:"schema_version"`
	CreatedAt      string           `json:"created_at"`
	Database       string           `json:"database"`
	VectorTable    string           `json:"vector_table"`
	EmbeddingModel string           `json:"embedding_model"`
	Dimension      int              `json:"embedding_dimension"`
	Documents      []SourceDocument `json:"documents"`
	Chunks         []IndexedChunk   `json:"chunks"`
}

type QuestionCase struct {
	ID                     string         `json:"id"`
	Question               string         `json:"question"`
	RetrievalKeywords      []string       `json:"retrieval_keywords"`
	FileIDs                []string       `json:"file_ids,omitempty"`
	RelevantDocuments      []string       `json:"relevant_documents"`
	RelevantEvidence       []string       `json:"relevant_evidence"`
	ExpectedAnswerKeywords []string       `json:"expected_answer_keywords"`
	ExpectedAnswerable     *bool          `json:"expected_answerable,omitempty"`
	Metadata               map[string]any `json:"metadata,omitempty"`
}

type ChunkResult struct {
	Rank            int       `json:"rank"`
	ChunkID         string    `json:"chunk_id"`
	FileID          string    `json:"file_id"`
	FileName        string    `json:"file_name"`
	PageNumber      int       `json:"page_number,omitempty"`
	Score           float64   `json:"score"`
	Routes          []string  `json:"routes"`
	Content         string    `json:"content"`
	SemanticModelID int64     `json:"semantic_model_id,omitempty"`
	Level           string    `json:"level,omitempty"`
	ChunkIndex      *int      `json:"chunk_index,omitempty"`
	ChunkIndexScope string    `json:"chunk_index_scope,omitempty"`
	ParentIndex     *int      `json:"parent_index,omitempty"`
	ChunkStart      *int      `json:"chunk_start,omitempty"`
	ChunkEnd        *int      `json:"chunk_end,omitempty"`
	SourceURI       string    `json:"source_uri,omitempty"`
	ImageFileID     string    `json:"image_file_id,omitempty"`
	PageImageFileID string    `json:"page_image_file_id,omitempty"`
	BBox            []float64 `json:"bbox,omitempty"`
	ObjectID        string    `json:"object_id,omitempty"`
	ObjectKind      string    `json:"object_kind,omitempty"`
	Scope           string    `json:"scope,omitempty"`
	ChunkType       string    `json:"chunk_type,omitempty"`
	BlockUUID       string    `json:"block_uuid,omitempty"`
}

type chunkLocation struct {
	PageNumber int
	SourceURI  string
	FileName   string
}

type CaseMetrics struct {
	SourceRecall          float64            `json:"source_recall"`
	EvidenceRecall        float64            `json:"evidence_recall"`
	ReciprocalRank        float64            `json:"reciprocal_rank"`
	RecallAtK             map[string]float64 `json:"source_recall_at_k"`
	AnswerabilityAccuracy *float64           `json:"answerability_accuracy,omitempty"`
	AnswerKeywordScore    *float64           `json:"answer_keyword_recall,omitempty"`
}

type RunResult struct {
	Case                       QuestionCase       `json:"case"`
	Repeat                     int                `json:"repeat"`
	StartedAt                  string             `json:"started_at"`
	EndedAt                    string             `json:"ended_at"`
	Status                     string             `json:"status"`
	RetrievalLatencyMS         float64            `json:"retrieval_latency_ms"`
	StageLatencyMS             map[string]float64 `json:"stage_latency_ms"`
	GenerationLatency          *float64           `json:"generation_latency_ms,omitempty"`
	GenerationProvider         string             `json:"generation_provider,omitempty"`
	GenerationModel            string             `json:"generation_model,omitempty"`
	GenerationContextBytes     int                `json:"generation_context_bytes,omitempty"`
	GenerationContextChunks    int                `json:"generation_context_chunks,omitempty"`
	GenerationContextTruncated bool               `json:"generation_context_truncated,omitempty"`
	Routes                     []string           `json:"routes"`
	EmbeddingModel             string             `json:"embedding_model"`
	Chunks                     []ChunkResult      `json:"chunks"`
	Answer                     string             `json:"answer,omitempty"`
	Metrics                    CaseMetrics        `json:"metrics"`
	Error                      string             `json:"error,omitempty"`
}

type Summary struct {
	SchemaVersion             string             `json:"schema_version"`
	CreatedAt                 string             `json:"created_at"`
	Attempts                  int                `json:"attempts"`
	SuccessfulAttempts        int                `json:"successful_attempts"`
	MeanSourceRecall          float64            `json:"mean_source_recall"`
	MeanEvidenceRecall        float64            `json:"mean_evidence_recall"`
	MeanReciprocalRank        float64            `json:"mean_reciprocal_rank"`
	MeanAnswerabilityAccuracy *float64           `json:"mean_answerability_accuracy,omitempty"`
	MeanAnswerKeywordRecall   *float64           `json:"mean_answer_keyword_recall,omitempty"`
	RetrievalLatencyMeanMS    float64            `json:"retrieval_latency_mean_ms"`
	RetrievalLatencyP50MS     float64            `json:"retrieval_latency_p50_ms"`
	RetrievalLatencyP95MS     float64            `json:"retrieval_latency_p95_ms"`
	StageLatencyMeanMS        map[string]float64 `json:"stage_latency_mean_ms"`
	StageLatencyP95MS         map[string]float64 `json:"stage_latency_p95_ms"`
	GenerationLatencyMeanMS   *float64           `json:"generation_latency_mean_ms,omitempty"`
	GenerationLatencyP95MS    *float64           `json:"generation_latency_p95_ms,omitempty"`
}

type openAIEmbeddingClient struct {
	config EndpointConfig
	client *http.Client
}

type requestRetryPolicy struct {
	MaxAttempts              int
	BaseDelay                time.Duration
	MaxDelay                 time.Duration
	MethodNotAllowedMinDelay time.Duration
}

type apiHTTPError struct {
	StatusCode int
	Body       string
	RetryAfter time.Duration
}

func (e *apiHTTPError) Error() string {
	return fmt.Sprintf("API_ERROR: HTTP %d: %s", e.StatusCode, e.Body)
}

type apiRequestError struct {
	URL string
	Err error
}

func (e *apiRequestError) Error() string {
	return fmt.Sprintf("API_ERROR: request %s: %v", e.URL, e.Err)
}

func (e *apiRequestError) Unwrap() error { return e.Err }

type hashEmbeddingClient struct {
	dimension int
}

type matrixOneExecutor struct {
	db *sql.DB
}

type timingRecorder struct {
	mu     sync.Mutex
	values map[string]time.Duration
}

type timedSQLExecutor struct {
	next                 knowledge.SQLExecutor
	recorder             *timingRecorder
	likeFullTextFallback bool
}

type timedEmbeddingService struct {
	next     knowledge.EmbeddingService
	recorder *timingRecorder
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "ingest":
		err = ingestCommand(os.Args[2:])
	case "run":
		err = runCommand(os.Args[2:])
	case "pipeline":
		err = pipelineCommand(os.Args[2:])
	case "ask":
		err = askCommand(os.Args[2:])
	case "check":
		err = checkCommand(os.Args[2:])
	case "mmdocir-ingest":
		err = mmdocirOfficialIngestCommand(os.Args[2:])
	case "mmdocir-run":
		err = mmdocirOfficialRunCommand(os.Args[2:])
	case "mmdocir-qa":
		err = mmdocirQACommand(os.Args[2:])
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: local-matrixflow-rag <check|ingest|run|ask|pipeline|mmdocir-ingest|mmdocir-run|mmdocir-qa> [flags]")
}

type commonFlags struct {
	configPath string
	runDir     string
}

func addCommonFlags(fs *flag.FlagSet, common *commonFlags) {
	fs.StringVar(&common.configPath, "config", "config.local.json", "benchmark configuration JSON")
	fs.StringVar(&common.runDir, "run", "runs/local-product-rag", "artifact root; each invocation creates a timestamped child directory")
}

func ingestCommand(args []string) error {
	fs := flag.NewFlagSet("ingest", flag.ContinueOnError)
	var common commonFlags
	var source, documents, resumeProgress string
	var force bool
	addCommonFlags(fs, &common)
	fs.StringVar(&source, "source", "data/documents", "document directory")
	fs.StringVar(&documents, "documents", "", "MatrixFlow parser documents.jsonl; when set, --source is ignored")
	fs.BoolVar(&force, "force", false, "replace all rows in the benchmark vector table")
	fs.StringVar(&resumeProgress, "resume-progress", "", "prior ingest-progress.json to resume without re-embedding committed rows")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if force && strings.TrimSpace(resumeProgress) != "" {
		return errors.New("--force and --resume-progress cannot be used together")
	}
	cfg, err := loadConfig(common.configPath)
	if err != nil {
		return err
	}
	runDir, err := allocateRunDir(common.runDir, time.Now())
	if err != nil {
		return err
	}
	fmt.Printf("run_dir=%s\n", runDir)
	if strings.TrimSpace(documents) != "" {
		_, err = ingestParsedDocuments(context.Background(), cfg, documents, runDir, force, resumeProgress)
	} else {
		_, err = ingestCorpus(context.Background(), cfg, source, runDir, force, resumeProgress)
	}
	return err
}

func runCommand(args []string) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	var common commonFlags
	var dataset string
	var repeats, maxHits, attemptTimeoutSeconds int
	addCommonFlags(fs, &common)
	fs.StringVar(&dataset, "dataset", "data/questions.jsonl", "question dataset JSONL")
	fs.IntVar(&repeats, "repeats", 1, "repeats per question")
	fs.IntVar(&maxHits, "max-hits", 10, "candidate hits per keyword and route")
	fs.IntVar(&attemptTimeoutSeconds, "attempt-timeout-seconds", 300, "hard timeout for one retrieval+generation attempt; 0 disables")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := loadConfig(common.configPath)
	if err != nil {
		return err
	}
	runDir, err := allocateRunDir(common.runDir, time.Now())
	if err != nil {
		return err
	}
	fmt.Printf("run_dir=%s\n", runDir)
	return runDataset(context.Background(), cfg, dataset, runDir, repeats, maxHits, attemptTimeoutSeconds)
}

func askCommand(args []string) error {
	fs := flag.NewFlagSet("ask", flag.ContinueOnError)
	var common commonFlags
	var question string
	addCommonFlags(fs, &common)
	fs.StringVar(&question, "question", "", "knowledge-base question")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(question) == "" {
		return errors.New("--question is required")
	}
	cfg, err := loadConfig(common.configPath)
	if err != nil {
		return err
	}
	runDir, err := allocateRunDir(common.runDir, time.Now())
	if err != nil {
		return err
	}
	fmt.Printf("run_dir=%s\n", runDir)
	result, err := runExploreQuestion(context.Background(), cfg, question)
	if err != nil {
		return err
	}
	if err := writeJSON(filepath.Join(runDir, "answer.json"), result); err != nil {
		return err
	}
	fmt.Println(result.Answer)
	return nil
}

func pipelineCommand(args []string) error {
	fs := flag.NewFlagSet("pipeline", flag.ContinueOnError)
	var common commonFlags
	var source, documents, dataset string
	var repeats, maxHits, attemptTimeoutSeconds int
	var force bool
	addCommonFlags(fs, &common)
	fs.StringVar(&source, "source", "data/documents", "document directory")
	fs.StringVar(&documents, "documents", "", "MatrixFlow parser documents.jsonl; when set, --source is ignored")
	fs.StringVar(&dataset, "dataset", "data/questions.jsonl", "question dataset JSONL")
	fs.IntVar(&repeats, "repeats", 1, "repeats per question")
	fs.IntVar(&maxHits, "max-hits", 10, "candidate hits per keyword and route")
	fs.IntVar(&attemptTimeoutSeconds, "attempt-timeout-seconds", 300, "hard timeout for one retrieval+generation attempt; 0 disables")
	fs.BoolVar(&force, "force", false, "replace all rows in the benchmark vector table")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := loadConfig(common.configPath)
	if err != nil {
		return err
	}
	runDir, err := allocateRunDir(common.runDir, time.Now())
	if err != nil {
		return err
	}
	fmt.Printf("run_dir=%s\n", runDir)
	if strings.TrimSpace(documents) != "" {
		if _, err := ingestParsedDocuments(context.Background(), cfg, documents, runDir, force, ""); err != nil {
			return err
		}
	} else {
		if _, err := ingestCorpus(context.Background(), cfg, source, runDir, force, ""); err != nil {
			return err
		}
	}
	return runDataset(context.Background(), cfg, dataset, runDir, repeats, maxHits, attemptTimeoutSeconds)
}

func allocateRunDir(root string, now time.Time) (string, error) {
	root = filepath.Clean(strings.TrimSpace(root))
	if root == "." {
		return "", errors.New("run artifact root must not be empty")
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return "", fmt.Errorf("create run artifact root: %w", err)
	}
	stamp := now.Format("20060102-150405.000")
	for sequence := 0; ; sequence++ {
		name := stamp
		if sequence > 0 {
			name += fmt.Sprintf("-%02d", sequence)
		}
		candidate := filepath.Join(root, name)
		err := os.Mkdir(candidate, 0o755)
		if err == nil {
			return candidate, nil
		}
		if !errors.Is(err, os.ErrExist) {
			return "", fmt.Errorf("create run artifact directory: %w", err)
		}
	}
}

func checkCommand(args []string) error {
	fs := flag.NewFlagSet("check", flag.ContinueOnError)
	var configPath string
	fs.StringVar(&configPath, "config", "config.local.json", "benchmark configuration JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	embedder, err := newEmbedder(cfg.Embedding)
	if err != nil {
		return err
	}
	vectors, err := embedder.CreateEmbedding(context.Background(), cfg.Workspace, cfg.Embedding.Model, []string{"MatrixFlow RAG health check"})
	if err != nil {
		return fmt.Errorf("embedding check: %w", err)
	}
	if len(vectors) != 1 || len(vectors[0]) == 0 {
		return errors.New("embedding check returned an empty vector")
	}
	db, err := openBenchmarkDB(context.Background(), cfg, len(vectors[0]), false)
	if err != nil {
		return err
	}
	defer db.Close()
	fmt.Printf("ok database=%s table=%s embedding_dimension=%d\n", cfg.MatrixOne.Database, cfg.MatrixOne.VectorTable, len(vectors[0]))
	return nil
}

func loadConfig(path string) (Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read config: %w", err)
	}
	var cfg Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return Config{}, fmt.Errorf("decode config: %w", err)
	}
	var rawConfig map[string]json.RawMessage
	if err := json.Unmarshal(raw, &rawConfig); err != nil {
		return Config{}, fmt.Errorf("decode config keys: %w", err)
	}
	if cfg.Workspace == "" {
		cfg.Workspace = "local-rag-benchmark"
	}
	if cfg.MatrixFlowRoot == "" {
		cfg.MatrixFlowRoot = filepath.Clean("../../../../matrixflow")
	}
	if cfg.ChunkSize <= 0 {
		cfg.ChunkSize = 512
	}
	if _, explicitlyConfigured := rawConfig["chunk_overlap"]; !explicitlyConfigured {
		// rag-ingest-default-v1.yaml: .vars.chunk_overlap // 64
		cfg.Overlap = 64
	}
	if cfg.Overlap < 0 || cfg.Overlap >= cfg.ChunkSize {
		return Config{}, errors.New("chunk_overlap must be >= 0 and smaller than chunk_size")
	}
	if cfg.SectionSize <= 0 {
		cfg.SectionSize = 5
	}
	// Keep the production insert batch size. Embedding request size is
	// configurable because OpenAI-compatible endpoints advertise different
	// request-count limits. The input-byte limit remains enforced by
	// splitEmbeddingInputsForLocalRAG.
	if cfg.EmbeddingBatchSize <= 0 {
		cfg.EmbeddingBatchSize = 64
	}
	if cfg.EmbeddingConcurrency <= 0 {
		cfg.EmbeddingConcurrency = 1
	}
	if cfg.EmbeddingConcurrency > 16 {
		return Config{}, errors.New("embedding_concurrency must be between 1 and 16")
	}
	cfg.InsertBatchSize = 50
	if cfg.MatrixOne.DSN == "" || cfg.MatrixOne.Database == "" || cfg.MatrixOne.VectorTable == "" {
		return Config{}, errors.New("matrixone.dsn, database, and vector_table are required")
	}
	if !identifierPattern.MatchString(cfg.MatrixOne.Database) || !identifierPattern.MatchString(cfg.MatrixOne.VectorTable) {
		return Config{}, errors.New("database and vector_table must be simple SQL identifiers")
	}
	if cfg.Embedding.Mode == "" {
		cfg.Embedding.Mode = "openai"
	}
	if cfg.Embedding.Model == "" {
		// rag-ingest-default-v1.yaml: .vars.embedding_model // BAAI/bge-m3
		cfg.Embedding.Model = "BAAI/bge-m3"
	}
	if strings.EqualFold(cfg.Embedding.Mode, "taas") {
		if cfg.Embedding.BaseURL == "" {
			cfg.Embedding.BaseURL = taasAPIBaseURL
		}
		if cfg.Embedding.APIKeyEnv == "" {
			cfg.Embedding.APIKeyEnv = taasAPIKeyEnvName
		}
		if cfg.Embedding.RetryMaxAttempts <= 0 {
			// TaaS has occasionally returned a transient 405 security-gateway
			// page during long embedding runs. Retry a bounded number of times;
			// a persistent error still aborts the run.
			cfg.Embedding.RetryMaxAttempts = 4
		}
		if cfg.Embedding.RetryBackoffSeconds <= 0 {
			cfg.Embedding.RetryBackoffSeconds = 5
		}
	}
	if strings.EqualFold(cfg.Embedding.Mode, "maas") {
		if cfg.Embedding.BaseURL == "" {
			cfg.Embedding.BaseURL = maasAPIBaseURL
		}
		if cfg.Embedding.APIKeyEnv == "" {
			cfg.Embedding.APIKeyEnv = maasAPIKeyEnvName
		}
		if cfg.Embedding.RetryMaxAttempts <= 0 {
			cfg.Embedding.RetryMaxAttempts = 4
		}
		if cfg.Embedding.RetryBackoffSeconds <= 0 {
			cfg.Embedding.RetryBackoffSeconds = 5
		}
	}
	if cfg.Embedding.Dimension <= 0 {
		if strings.EqualFold(cfg.Embedding.Mode, "hash") {
			cfg.Embedding.Dimension = 256
		} else {
			return Config{}, errors.New("embedding.dimension must be positive")
		}
	}
	if cfg.Embedding.TimeoutSeconds <= 0 {
		cfg.Embedding.TimeoutSeconds = 60
	}
	if cfg.Generation.TimeoutSeconds <= 0 {
		cfg.Generation.TimeoutSeconds = 120
	}
	if cfg.Generation.RetryMaxAttempts <= 0 {
		cfg.Generation.RetryMaxAttempts = 3
	}
	if cfg.Generation.RetryBackoffSeconds <= 0 {
		cfg.Generation.RetryBackoffSeconds = 5
	}
	if strings.EqualFold(cfg.Generation.Provider, "taas") {
		if cfg.Generation.BaseURL == "" {
			cfg.Generation.BaseURL = taasAPIBaseURL
		}
		if cfg.Generation.APIKeyEnv == "" {
			cfg.Generation.APIKeyEnv = taasAPIKeyEnvName
		}
	}
	if strings.EqualFold(cfg.Generation.Provider, "maas") {
		if cfg.Generation.BaseURL == "" {
			cfg.Generation.BaseURL = maasAPIBaseURL
		}
		if cfg.Generation.APIKeyEnv == "" {
			cfg.Generation.APIKeyEnv = maasAPIKeyEnvName
		}
	}
	if cfg.Generation.Fallback != nil {
		if strings.EqualFold(cfg.Generation.Fallback.Provider, "taas") {
			if cfg.Generation.Fallback.BaseURL == "" {
				cfg.Generation.Fallback.BaseURL = taasAPIBaseURL
			}
			if cfg.Generation.Fallback.APIKeyEnv == "" {
				cfg.Generation.Fallback.APIKeyEnv = taasAPIKeyEnvName
			}
		}
		if strings.EqualFold(cfg.Generation.Fallback.Provider, "maas") {
			if cfg.Generation.Fallback.BaseURL == "" {
				cfg.Generation.Fallback.BaseURL = maasAPIBaseURL
			}
			if cfg.Generation.Fallback.APIKeyEnv == "" {
				cfg.Generation.Fallback.APIKeyEnv = maasAPIKeyEnvName
			}
		}
		if cfg.Generation.Fallback.TimeoutSeconds <= 0 {
			cfg.Generation.Fallback.TimeoutSeconds = 180
		}
		if cfg.Generation.Fallback.RetryMaxAttempts <= 0 {
			cfg.Generation.Fallback.RetryMaxAttempts = 2
		}
		if cfg.Generation.Fallback.RetryBackoffSeconds <= 0 {
			cfg.Generation.Fallback.RetryBackoffSeconds = 5
		}
	}
	return cfg, nil
}

func ingestCorpus(ctx context.Context, cfg Config, sourceDir, runDir string, force bool, resumeProgress string) (*IngestState, error) {
	documents, err := loadDocuments(sourceDir)
	if err != nil {
		return nil, err
	}
	parsed := make([]parsedDocument, 0, len(documents))
	for index, document := range documents {
		parsed = append(parsed, parsedDocument{
			Content: document.Text,
			Type:    "text",
			Metadata: map[string]any{
				"file_id":          document.FileID,
				"raw_file_id":      document.FileID,
				"file_name":        document.Path,
				"source_file_name": document.Path,
				"source_uri":       "benchmark://" + document.Path,
				"document_index":   index,
			},
		})
	}
	return ingestProductDocuments(ctx, cfg, parsed, runDir, force, documents, resumeProgress)
}

func loadDocuments(root string) ([]SourceDocument, error) {
	var documents []SourceDocument
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		ext := strings.ToLower(filepath.Ext(entry.Name()))
		if ext != ".md" && ext != ".txt" {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		text := strings.TrimSpace(string(raw))
		if text == "" {
			return nil
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		documents = append(documents, SourceDocument{
			FileID: stableID("file", relative),
			Path:   relative,
			SHA256: sha256Text(text),
			Text:   text,
		})
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("load documents: %w", err)
	}
	sort.Slice(documents, func(i, j int) bool { return documents[i].Path < documents[j].Path })
	if len(documents) == 0 {
		return nil, fmt.Errorf("no non-empty .md or .txt documents under %s", root)
	}
	return documents, nil
}

func newEmbedder(cfg EndpointConfig) (knowledge.EmbeddingService, error) {
	switch strings.ToLower(strings.TrimSpace(cfg.Mode)) {
	case "hash":
		if cfg.Dimension <= 0 {
			cfg.Dimension = 256
		}
		return hashEmbeddingClient{dimension: cfg.Dimension}, nil
	case "openai", "taas", "maas":
		if cfg.BaseURL == "" || cfg.Model == "" {
			return nil, fmt.Errorf("%s embedding mode requires base_url and model", cfg.Mode)
		}
		direct := isDirectProvider(cfg.Mode)
		transport := newHTTPTransport(direct)
		if direct {
			// Long-running TaaS and MaaS embedding jobs have both observed stale
			// keep-alive channels after idle periods. A fresh direct connection per
			// request avoids turning a sub-second embedding into repeated network
			// timeouts and also keeps the system proxy out of the provider path.
			transport.DisableKeepAlives = true
			transport.MaxIdleConns = 0
			transport.MaxIdleConnsPerHost = 0
		}
		return &openAIEmbeddingClient{
			config: cfg,
			client: &http.Client{Timeout: time.Duration(cfg.TimeoutSeconds) * time.Second, Transport: transport},
		}, nil
	default:
		return nil, fmt.Errorf("unsupported embedding mode %q", cfg.Mode)
	}
}

func isDirectProvider(provider string) bool {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "taas", "maas", "deepseek", "deepseek-official", "dsv4f":
		return true
	default:
		return false
	}
}

// newHTTPTransport preserves the host's normal HTTP settings while allowing
// provider-specific routing. TaaS calls pass direct=true so HTTP(S)_PROXY
// inherited by the Codex/terminal process is not used. Other providers retain
// the default environment-driven proxy behavior.
func newHTTPTransport(direct bool) *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if direct {
		transport.Proxy = nil
	}
	return transport
}

func (c *openAIEmbeddingClient) CreateEmbedding(ctx context.Context, _ string, model string, texts []string) ([][]float64, error) {
	if model == "" {
		model = c.config.Model
	}
	payload := map[string]any{"model": model, "input": texts, "encoding_format": "float"}
	var response struct {
		Data []struct {
			Index     int       `json:"index"`
			Embedding []float32 `json:"embedding"`
		} `json:"data"`
	}
	if err := postOpenAIJSONWithRetry(
		ctx,
		c.client,
		c.config.BaseURL,
		"/embeddings",
		c.config.APIKeyEnv,
		payload,
		&response,
		requestRetryPolicy{
			MaxAttempts:              c.config.RetryMaxAttempts,
			BaseDelay:                time.Duration(c.config.RetryBackoffSeconds * float64(time.Second)),
			MaxDelay:                 3 * time.Minute,
			MethodNotAllowedMinDelay: 30 * time.Second,
		},
	); err != nil {
		return nil, err
	}
	out := make([][]float64, len(texts))
	for _, item := range response.Data {
		if item.Index < 0 || item.Index >= len(out) {
			return nil, fmt.Errorf("embedding response index %d out of range", item.Index)
		}
		out[item.Index] = make([]float64, len(item.Embedding))
		for index, value := range item.Embedding {
			// MatrixFlow's SDK response is float32; preserve the same conversion
			// before values reach the VECF64 column.
			out[item.Index][index] = float64(value)
		}
	}
	for i, embedding := range out {
		if len(embedding) == 0 {
			return nil, fmt.Errorf("embedding response missing index %d", i)
		}
	}
	return out, nil
}

func (c hashEmbeddingClient) CreateEmbedding(_ context.Context, _, _ string, texts []string) ([][]float64, error) {
	out := make([][]float64, 0, len(texts))
	for _, text := range texts {
		vector := make([]float64, c.dimension)
		for _, token := range tokenize(text) {
			digest := sha256.Sum256([]byte(token))
			index := int(digest[0])<<8 | int(digest[1])
			sign := 1.0
			if digest[2]&1 == 1 {
				sign = -1
			}
			vector[index%c.dimension] += sign
		}
		var norm float64
		for _, value := range vector {
			norm += value * value
		}
		if norm > 0 {
			norm = math.Sqrt(norm)
			for i := range vector {
				vector[i] /= norm
			}
		}
		out = append(out, vector)
	}
	return out, nil
}

func tokenize(text string) []string {
	text = strings.ToLower(text)
	var tokens []string
	var current []rune
	flush := func() {
		if len(current) > 0 {
			tokens = append(tokens, string(current))
			current = nil
		}
	}
	var han []rune
	flushHan := func() {
		if len(han) == 1 {
			tokens = append(tokens, string(han))
		} else if len(han) > 1 {
			for i := 0; i < len(han)-1; i++ {
				tokens = append(tokens, string(han[i:i+2]))
			}
		}
		han = nil
	}
	for _, r := range text {
		switch {
		case unicode.In(r, unicode.Han):
			flush()
			han = append(han, r)
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			flushHan()
			current = append(current, r)
		default:
			flush()
			flushHan()
		}
	}
	flush()
	flushHan()
	return tokens
}

func openBenchmarkDB(ctx context.Context, cfg Config, dimension int, rebuild bool) (*sql.DB, error) {
	if dimension <= 0 {
		return nil, fmt.Errorf("embedding dimension must be positive, got %d", dimension)
	}
	parsed, err := mysqlDriver.ParseDSN(cfg.MatrixOne.DSN)
	if err != nil {
		return nil, fmt.Errorf("parse MatrixOne DSN: %w", err)
	}
	parsed.DBName = ""
	admin, err := sql.Open("mysql", parsed.FormatDSN())
	if err != nil {
		return nil, err
	}
	if err := admin.PingContext(ctx); err != nil {
		admin.Close()
		return nil, fmt.Errorf("connect MatrixOne: %w", err)
	}
	if _, err := admin.ExecContext(ctx, "CREATE DATABASE IF NOT EXISTS `"+cfg.MatrixOne.Database+"`"); err != nil {
		admin.Close()
		return nil, fmt.Errorf("create benchmark database: %w", err)
	}
	admin.Close()
	parsed.DBName = cfg.MatrixOne.Database
	db, err := sql.Open("mysql", parsed.FormatDSN())
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("connect benchmark database: %w", err)
	}
	table := "`" + cfg.MatrixOne.VectorTable + "`"
	if rebuild {
		if _, err := db.ExecContext(ctx, "DROP TABLE IF EXISTS "+table); err != nil {
			db.Close()
			return nil, fmt.Errorf("rebuild vector table: %w", err)
		}
	}
	if cfg.SkipVectorIndexRebuild && !rebuild {
		var tableCount int
		if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
			WHERE table_schema = DATABASE() AND table_name = ?`, cfg.MatrixOne.VectorTable).Scan(&tableCount); err != nil {
			db.Close()
			return nil, fmt.Errorf("check existing vector table: %w", err)
		}
		if tableCount == 1 {
			return db, nil
		}
	}
	if err := workitems.EnsureVectorTableForLocalRAG(db, cfg.MatrixOne.VectorTable, dimension); err != nil {
		db.Close()
		return nil, fmt.Errorf("ensure MatrixFlow vector table: %w", err)
	}
	return db, nil
}

// openIngestDB temporarily removes the IVFFLAT index before a large write.
// MatrixOne otherwise updates the vector index on every 50-row production
// batch, which makes a full rebuild increasingly slow. The caller recreates
// it once all rows have been committed.
func openIngestDB(ctx context.Context, cfg Config, dimension int, rebuild bool) (*sql.DB, error) {
	if cfg.SkipVectorIndexRebuild && !rebuild {
		db, err := openExistingIngestDB(ctx, cfg)
		if err != nil {
			return nil, err
		}
		if cfg.SkipFullTextIndexDuringIngest {
			if err := dropFullTextIndex(ctx, db, cfg.MatrixOne.VectorTable); err != nil {
				db.Close()
				return nil, err
			}
		}
		return db, nil
	}
	db, err := openBenchmarkDB(ctx, cfg, dimension, rebuild)
	if err != nil {
		return nil, err
	}
	// Discover the actual IVFFLAT index name instead of assuming the preferred
	// name. This makes fresh and resumed ingests robust to older tables that
	// still carry the cosine index name.
	var indexName string
	err = db.QueryRowContext(ctx, `SELECT index_name FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name = ?
		  AND column_name = 'embedding' AND LOWER(index_type) = 'ivfflat'
		LIMIT 1`, cfg.MatrixOne.VectorTable).Scan(&indexName)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return db, nil
	case err != nil:
		db.Close()
		return nil, fmt.Errorf("check vector index during ingest: %w", err)
	}
	if !identifierPattern.MatchString(indexName) {
		db.Close()
		return nil, fmt.Errorf("vector index has unsafe name %q", indexName)
	}
	statement := "ALTER TABLE `" + cfg.MatrixOne.VectorTable + "` DROP INDEX `" + indexName + "`"
	if _, err := db.ExecContext(ctx, statement); err != nil {
		db.Close()
		return nil, fmt.Errorf("defer vector index during ingest: %w", err)
	}
	return db, nil
}

// openExistingIngestDB opens a partially populated table without running the
// normal ensure path first. The normal path creates an IVFFLAT index, which is
// exactly what a resumed bulk ingest must defer until all remaining rows have
// been written.
func openExistingIngestDB(ctx context.Context, cfg Config) (*sql.DB, error) {
	parsed, err := mysqlDriver.ParseDSN(cfg.MatrixOne.DSN)
	if err != nil {
		return nil, fmt.Errorf("parse MatrixOne DSN: %w", err)
	}
	parsed.DBName = cfg.MatrixOne.Database
	db, err := sql.Open("mysql", parsed.FormatDSN())
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("connect resume database: %w", err)
	}

	var tableCount int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM information_schema.tables
		WHERE table_schema = DATABASE() AND table_name = ?`, cfg.MatrixOne.VectorTable).Scan(&tableCount); err != nil {
		db.Close()
		return nil, fmt.Errorf("check resume vector table: %w", err)
	}
	if tableCount != 1 {
		db.Close()
		return nil, fmt.Errorf("resume vector table %s.%s does not exist", cfg.MatrixOne.Database, cfg.MatrixOne.VectorTable)
	}

	var indexName string
	err = db.QueryRowContext(ctx, `SELECT index_name FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name = ?
		  AND column_name = 'embedding' AND LOWER(index_type) = 'ivfflat'
		LIMIT 1`, cfg.MatrixOne.VectorTable).Scan(&indexName)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return db, nil
	case err != nil:
		db.Close()
		return nil, fmt.Errorf("check resume vector index: %w", err)
	}
	if !identifierPattern.MatchString(indexName) {
		db.Close()
		return nil, fmt.Errorf("resume vector index has unsafe name %q", indexName)
	}
	statement := "ALTER TABLE `" + cfg.MatrixOne.VectorTable + "` DROP INDEX `" + indexName + "`"
	if _, err := db.ExecContext(ctx, statement); err != nil {
		db.Close()
		return nil, fmt.Errorf("defer existing vector index during resume: %w", err)
	}
	return db, nil
}

func dropFullTextIndex(ctx context.Context, db *sql.DB, tableName string) error {
	var indexName string
	err := db.QueryRowContext(ctx, `SELECT index_name FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name = ?
		  AND column_name = 'content' AND LOWER(index_type) = 'fulltext'
		LIMIT 1`, tableName).Scan(&indexName)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("check fulltext index during ingest: %w", err)
	}
	if !identifierPattern.MatchString(indexName) {
		return fmt.Errorf("fulltext index has unsafe name %q", indexName)
	}
	if _, err := db.ExecContext(ctx, "ALTER TABLE `"+tableName+"` DROP INDEX `"+indexName+"`"); err != nil {
		return fmt.Errorf("defer fulltext index during ingest: %w", err)
	}
	return nil
}

func ensureFullTextIndex(ctx context.Context, db *sql.DB, tableName string) error {
	var indexName string
	err := db.QueryRowContext(ctx, `SELECT index_name FROM information_schema.statistics
		WHERE table_schema = DATABASE() AND table_name = ?
		  AND column_name = 'content' AND LOWER(index_type) = 'fulltext'
		LIMIT 1`, tableName).Scan(&indexName)
	if err == nil {
		return nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("check fulltext index after ingest: %w", err)
	}
	if _, err := db.ExecContext(ctx, "ALTER TABLE `"+tableName+"` ADD FULLTEXT KEY `idx_content_ft` (`content`)"); err != nil {
		return fmt.Errorf("rebuild fulltext index after ingest: %w", err)
	}
	return nil
}

func (e matrixOneExecutor) ExecuteSQL(ctx context.Context, _ string, query string) (*knowledge.SQLExecutionResult, error) {
	rows, err := e.db.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	result := &knowledge.SQLExecutionResult{Columns: columns}
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err := rows.Scan(pointers...); err != nil {
			return nil, err
		}
		for i, value := range values {
			if raw, ok := value.([]byte); ok {
				values[i] = string(raw)
			}
		}
		result.Rows = append(result.Rows, values)
	}
	return result, rows.Err()
}

func (r *timingRecorder) reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.values = make(map[string]time.Duration)
}

func (r *timingRecorder) add(stage string, elapsed time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.values == nil {
		r.values = make(map[string]time.Duration)
	}
	r.values[stage] += elapsed
}

func (r *timingRecorder) milliseconds() map[string]float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	values := make(map[string]float64, len(r.values))
	for stage, elapsed := range r.values {
		values[stage] = float64(elapsed.Microseconds()) / 1000
	}
	return values
}

func (e timedSQLExecutor) ExecuteSQL(ctx context.Context, workspaceID, query string) (*knowledge.SQLExecutionResult, error) {
	started := time.Now()
	effectiveQuery := query
	if e.likeFullTextFallback && strings.Contains(strings.ToLower(query), "match(content)") {
		effectiveQuery = rewriteFullTextSQLToLike(query)
	}
	result, err := e.next.ExecuteSQL(ctx, workspaceID, effectiveQuery)
	stage := classifySQLStage(query)
	if effectiveQuery != query {
		stage = "fulltext_like_fallback"
	}
	e.recorder.add(stage, time.Since(started))
	return result, err
}

const fullTextMatchPrefix = "MATCH(content) AGAINST('"
const fullTextMatchSuffix = "' IN NATURAL LANGUAGE MODE)"

// rewriteFullTextSQLToLike keeps the product retrieval shape intact when a
// MatrixOne deployment cannot build/retain a FULLTEXT index. DocBench cases
// are file-scoped, so these LIKE predicates scan one document's chunks.
func rewriteFullTextSQLToLike(query string) string {
	var out strings.Builder
	for cursor := 0; cursor < len(query); {
		relative := strings.Index(query[cursor:], fullTextMatchPrefix)
		if relative < 0 {
			out.WriteString(query[cursor:])
			break
		}
		start := cursor + relative
		out.WriteString(query[cursor:start])
		literalStart := start + len(fullTextMatchPrefix)
		literalEnd := -1
		for index := literalStart; index < len(query); index++ {
			if query[index] != '\'' {
				continue
			}
			if index+1 < len(query) && query[index+1] == '\'' {
				index++
				continue
			}
			if strings.HasPrefix(query[index:], fullTextMatchSuffix) {
				literalEnd = index
				break
			}
		}
		if literalEnd < 0 {
			out.WriteString(query[start:])
			break
		}
		phrase := strings.ReplaceAll(query[literalStart:literalEnd], "''", "'")
		out.WriteString(likeScoreExpression(phrase))
		cursor = literalEnd + len(fullTextMatchSuffix)
	}
	return out.String()
}

func likeScoreExpression(phrase string) string {
	terms := make([]string, 0, 16)
	seen := map[string]struct{}{}
	addTerm := func(value string) {
		value = strings.TrimSpace(value)
		if value == "" || len([]rune(value)) < 2 {
			return
		}
		if _, exists := seen[value]; exists {
			return
		}
		seen[value] = struct{}{}
		terms = append(terms, value)
	}
	for _, token := range strings.FieldsFunc(phrase, func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsDigit(r) }) {
		addTerm(token)
		if len(terms) >= 16 {
			break
		}
	}
	if len(terms) == 0 {
		addTerm(phrase)
	}
	parts := make([]string, 0, len(terms))
	for _, term := range terms {
		escaped := strings.ReplaceAll(term, "\\", "\\\\")
		escaped = strings.ReplaceAll(escaped, "'", "''")
		escaped = strings.ReplaceAll(escaped, "%", "\\%")
		escaped = strings.ReplaceAll(escaped, "_", "\\_")
		parts = append(parts, "CASE WHEN content LIKE '%"+escaped+"%' THEN 1 ELSE 0 END")
	}
	if len(parts) == 0 {
		return "0"
	}
	return "(" + strings.Join(parts, " + ") + ")"
}

func classifySQLStage(query string) string {
	normalized := strings.ToLower(query)
	switch {
	case strings.Contains(normalized, "show columns"), strings.Contains(normalized, "show index"):
		return "schema_inspection"
	case strings.Contains(normalized, "match("):
		return "fulltext_search"
	case strings.Contains(normalized, "cosine_distance"), strings.Contains(normalized, "l2_distance"):
		return "vector_search"
	default:
		return "evidence_expansion"
	}
}

func (e timedEmbeddingService) CreateEmbedding(ctx context.Context, workspaceID, model string, texts []string) ([][]float64, error) {
	started := time.Now()
	result, err := e.next.CreateEmbedding(ctx, workspaceID, model, texts)
	e.recorder.add("embedding", time.Since(started))
	return result, err
}

func runDataset(ctx context.Context, cfg Config, datasetPath, runDir string, repeats, maxHits, attemptTimeoutSeconds int) error {
	if repeats < 1 || maxHits < 1 {
		return errors.New("repeats and max-hits must be positive")
	}
	cases, err := loadQuestions(datasetPath)
	if err != nil {
		return err
	}
	embedder, err := newEmbedder(cfg.Embedding)
	if err != nil {
		return err
	}
	db, err := openBenchmarkDB(ctx, cfg, cfg.Embedding.Dimension, false)
	if err != nil {
		return err
	}
	defer db.Close()
	recorder := &timingRecorder{}
	searcher := knowledgeservice.NewSearchRAGChunks(knowledgeservice.Deps{
		SQLExecutor:            timedSQLExecutor{next: matrixOneExecutor{db: db}, recorder: recorder, likeFullTextFallback: cfg.LikeFullTextFallback},
		Embedder:               timedEmbeddingService{next: embedder, recorder: recorder},
		DefaultRetrieverConfig: knowledge.RetrieverConfig{EmbeddingModel: cfg.Embedding.Model},
	})
	var results []RunResult
	resultsPath := filepath.Join(runDir, "results.jsonl")
	appendResult := func(result RunResult) error {
		results = append(results, result)
		return appendJSONL(resultsPath, result)
	}
	flushPartial := func() {
		summary := summarize(results)
		_ = writeJSON(filepath.Join(runDir, "summary.json"), summary)
		_ = writeReport(filepath.Join(runDir, "report.md"), summary)
	}
	attemptNumber := 0
	for _, item := range cases {
		keywords := compactStrings(item.RetrievalKeywords)
		if len(keywords) == 0 {
			keywords = []string{item.Question}
		}
		for repeat := 1; repeat <= repeats; repeat++ {
			attemptNumber++
			recorder.reset()
			started := time.Now()
			attemptCtx := ctx
			cancelAttempt := func() {}
			if attemptTimeoutSeconds > 0 {
				attemptCtx, cancelAttempt = context.WithTimeout(ctx, time.Duration(attemptTimeoutSeconds)*time.Second)
			}
			response, searchErr := searcher.Execute(attemptCtx, knowledge.SearchRAGChunksRequest{
				Scope: knowledge.WorkspaceScope{
					WorkspaceID:    cfg.Workspace,
					DBName:         cfg.MatrixOne.Database,
					VectorTable:    cfg.MatrixOne.VectorTable,
					EmbeddingModel: cfg.Embedding.Model,
				},
				Keywards: keywords,
				FileIDs:  effectiveRAGFileIDs(cfg, item),
				MaxHits:  maxHits,
				// Keep the benchmark's expansion work bounded by the requested
				// retrieval depth. Native expansion otherwise materializes every
				// matching row in a large parent range.
				MaxRows: maxHits,
			})
			result := RunResult{
				Case:               item,
				Repeat:             repeat,
				StartedAt:          started.UTC().Format(time.RFC3339Nano),
				EndedAt:            time.Now().UTC().Format(time.RFC3339Nano),
				RetrievalLatencyMS: float64(time.Since(started).Microseconds()) / 1000,
				StageLatencyMS:     recorder.milliseconds(),
				Status:             "ok",
			}
			if searchErr != nil {
				cancelAttempt()
				result.Status = "failed"
				result.Error = searchErr.Error()
				result.EndedAt = time.Now().UTC().Format(time.RFC3339Nano)
				if err := appendResult(result); err != nil {
					return err
				}
				fmt.Printf("attempt=%d/%d id=%s repeat=%d status=failed stage=retrieval\n", attemptNumber, len(cases)*repeats, item.ID, repeat)
				// Keep API failures in the initial-attempt denominator and move on
				// to the next question.  A Qianfan embedding cannot transparently
				// replace the BGE-M3 vector used by this index, so retrieval stays a
				// recorded fail rather than mixing vector spaces.
				flushPartial()
				continue
			}
			result.Routes = response.Routes
			result.EmbeddingModel = response.EmbeddingModel
			result.Chunks = normalizeChunkResults(response.Chunks)
			if err := enrichChunkLocations(attemptCtx, db, cfg.MatrixOne.VectorTable, result.Chunks); err != nil {
				cancelAttempt()
				result.Status = "failed"
				result.Error = fmt.Sprintf("load result chunk locations: %v", err)
				result.EndedAt = time.Now().UTC().Format(time.RFC3339Nano)
				if err := appendResult(result); err != nil {
					return err
				}
				fmt.Printf("attempt=%d/%d id=%s repeat=%d status=failed stage=enrichment\n", attemptNumber, len(cases)*repeats, item.ID, repeat)
				flushPartial()
				continue
			}
			result.Metrics = scoreCase(item, result.Chunks)
			if cfg.Generation.Enabled {
				generationChunks, contextBytes, contextTruncated := boundedGenerationChunks(result.Chunks, cfg.Generation.MaxContextBytes)
				result.GenerationContextBytes = contextBytes
				result.GenerationContextChunks = len(generationChunks)
				result.GenerationContextTruncated = contextTruncated
				generationStarted := time.Now()
				answer, generationProvider, generationModel, generationErr := generateAnswer(attemptCtx, cfg.Generation, item.Question, generationChunks)
				latency := float64(time.Since(generationStarted).Microseconds()) / 1000
				result.GenerationLatency = &latency
				if generationErr != nil {
					cancelAttempt()
					result.Status = "failed"
					result.Error = generationErr.Error()
					if err := appendResult(result); err != nil {
						return err
					}
					fmt.Printf("attempt=%d/%d id=%s repeat=%d status=failed stage=generation\n", attemptNumber, len(cases)*repeats, item.ID, repeat)
					flushPartial()
					continue
				} else {
					result.Answer = answer
					result.GenerationProvider = generationProvider
					result.GenerationModel = generationModel
					score := keywordRecall(answer, item.ExpectedAnswerKeywords)
					result.Metrics.AnswerKeywordScore = score
				}
			}
			cancelAttempt()
			result.EndedAt = time.Now().UTC().Format(time.RFC3339Nano)
			if err := appendResult(result); err != nil {
				return err
			}
			fmt.Printf("attempt=%d/%d id=%s repeat=%d status=%s\n", attemptNumber, len(cases)*repeats, item.ID, repeat, result.Status)
		}
	}
	// Results are appended after every attempt, so a killed process leaves a
	// usable raw ledger. Rewriting here also canonicalizes the file on normal
	// completion.
	if err := writeJSONL(resultsPath, results); err != nil {
		return err
	}
	summary := summarize(results)
	if err := writeJSON(filepath.Join(runDir, "summary.json"), summary); err != nil {
		return err
	}
	if err := writeReport(filepath.Join(runDir, "report.md"), summary); err != nil {
		return err
	}
	fmt.Printf("attempts=%d successful=%d source_recall=%.3f evidence_recall=%.3f p95_ms=%.2f\n",
		summary.Attempts, summary.SuccessfulAttempts, summary.MeanSourceRecall, summary.MeanEvidenceRecall, summary.RetrievalLatencyP95MS)
	return nil
}

// effectiveRAGFileIDs preserves a question's file scope unless the config
// explicitly declares that the question's IDs cover the entire benchmark
// corpus. In that case omitting the equivalent IN (...) predicate lets
// MatrixOne use the native vector/fulltext candidate path without evaluating a
// very large filter list on every query.
func effectiveRAGFileIDs(cfg Config, item QuestionCase) []string {
	fileIDs := compactStrings(item.FileIDs)
	if cfg.MatrixOne.SkipExhaustiveFileIDFilter && len(fileIDs) > 0 {
		return nil
	}
	return fileIDs
}

func loadQuestions(path string) ([]QuestionCase, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cases []QuestionCase
	for index, line := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var item QuestionCase
		if err := json.Unmarshal([]byte(line), &item); err != nil {
			return nil, fmt.Errorf("decode dataset line %d: %w", index+1, err)
		}
		if strings.TrimSpace(item.ID) == "" || strings.TrimSpace(item.Question) == "" {
			return nil, fmt.Errorf("dataset line %d requires id and question", index+1)
		}
		cases = append(cases, item)
	}
	if len(cases) == 0 {
		return nil, errors.New("dataset is empty")
	}
	return cases, nil
}

func normalizeChunkResults(hits []knowledge.RAGChunkHit) []ChunkResult {
	out := make([]ChunkResult, 0, len(hits))
	for index, hit := range hits {
		out = append(out, ChunkResult{
			Rank:            index + 1,
			ChunkID:         hit.ChunkID,
			FileID:          hit.FileID,
			FileName:        hit.FileName,
			PageNumber:      hit.PageNumber,
			Score:           hit.Score,
			Routes:          hit.Routes,
			Content:         hit.Content,
			SemanticModelID: hit.SemanticModelID,
			Level:           hit.Level,
			ChunkIndex:      hit.ChunkIndex,
			ChunkIndexScope: hit.ChunkIndexScope,
			ParentIndex:     hit.ParentIndex,
			ChunkStart:      hit.ChunkStart,
			ChunkEnd:        hit.ChunkEnd,
			SourceURI:       hit.SourceURI,
			ImageFileID:     hit.ImageFileID,
			PageImageFileID: hit.PageImageFileID,
			BBox:            hit.BBox,
			ObjectID:        hit.ObjectID,
			ObjectKind:      hit.ObjectKind,
			Scope:           hit.Scope,
			ChunkType:       hit.ChunkType,
			BlockUUID:       hit.BlockUUID,
		})
	}
	return out
}

// SearchRAGChunks intentionally projects a compact set of columns and, for
// compatibility with older vector tables, reads page_number from metadata.
// The official MMDocIR page table already has the canonical page_number
// column, but its legacy rows predate the metadata mirror.  Resolve only the
// returned chunk indexes, rather than scanning the entire table at startup.
func enrichChunkLocations(ctx context.Context, db *sql.DB, table string, chunks []ChunkResult) error {
	if db == nil {
		return errors.New("database is nil")
	}
	byFile := make(map[string][]int)
	for _, chunk := range chunks {
		if chunk.PageNumber != 0 || chunk.ChunkIndex == nil || strings.TrimSpace(chunk.FileID) == "" {
			continue
		}
		index := *chunk.ChunkIndex
		seen := false
		for _, existing := range byFile[chunk.FileID] {
			if existing == index {
				seen = true
				break
			}
		}
		if !seen {
			byFile[chunk.FileID] = append(byFile[chunk.FileID], index)
		}
	}
	for fileID, indexes := range byFile {
		placeholders := make([]string, len(indexes))
		args := make([]any, 0, len(indexes)+1)
		args = append(args, fileID)
		for i, index := range indexes {
			placeholders[i] = "?"
			args = append(args, index)
		}
		query := "SELECT chunk_index, page_number, meta FROM `" + table + "` WHERE level = 'chunk' AND file_id = ? AND chunk_index IN (" + strings.Join(placeholders, ",") + ")"
		rows, err := db.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		locations := make(map[int]chunkLocation)
		for rows.Next() {
			var chunkIndex int
			var pageNumber sql.NullInt64
			var rawMeta []byte
			if err := rows.Scan(&chunkIndex, &pageNumber, &rawMeta); err != nil {
				rows.Close()
				return err
			}
			location := chunkLocation{PageNumber: 0}
			if pageNumber.Valid {
				location.PageNumber = int(pageNumber.Int64)
			}
			var metadata map[string]any
			if len(rawMeta) > 0 {
				if err := json.Unmarshal(rawMeta, &metadata); err != nil {
					rows.Close()
					return err
				}
			}
			if location.PageNumber == 0 && metadata != nil {
				if value, ok := metadata["page_number"].(float64); ok {
					location.PageNumber = int(value)
				}
			}
			if metadata != nil {
				if value, ok := metadata["file_name"].(string); ok {
					location.FileName = value
				}
				if value, ok := metadata["source_uri"].(string); ok {
					location.SourceURI = value
				}
			}
			locations[chunkIndex] = location
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for index := range chunks {
			if chunks[index].FileID != fileID || chunks[index].ChunkIndex == nil {
				continue
			}
			location, ok := locations[*chunks[index].ChunkIndex]
			if !ok {
				continue
			}
			if chunks[index].PageNumber == 0 {
				chunks[index].PageNumber = location.PageNumber
			}
			if chunks[index].FileName == "" {
				chunks[index].FileName = location.FileName
			}
			if chunks[index].SourceURI == "" {
				chunks[index].SourceURI = location.SourceURI
			}
		}
	}
	return nil
}

func scoreCase(item QuestionCase, chunks []ChunkResult) CaseMetrics {
	metrics := CaseMetrics{RecallAtK: map[string]float64{}}
	metrics.SourceRecall = sourceRecall(item.RelevantDocuments, chunks)
	metrics.EvidenceRecall = evidenceRecall(item.RelevantEvidence, chunks)
	metrics.ReciprocalRank = reciprocalRank(item.RelevantDocuments, chunks)
	if item.ExpectedAnswerable != nil {
		value := 0.0
		if !*item.ExpectedAnswerable && len(chunks) == 0 {
			value = 1
		}
		if *item.ExpectedAnswerable && (metrics.SourceRecall > 0 || metrics.EvidenceRecall > 0) {
			value = 1
		}
		metrics.AnswerabilityAccuracy = &value
	}
	for _, k := range []int{1, 3, 5, 10} {
		end := min(k, len(chunks))
		metrics.RecallAtK[strconv.Itoa(k)] = sourceRecall(item.RelevantDocuments, chunks[:end])
	}
	return metrics
}

func sourceRecall(expected []string, chunks []ChunkResult) float64 {
	expected = compactStrings(expected)
	if len(expected) == 0 {
		return 1
	}
	hits := map[string]struct{}{}
	for _, chunk := range chunks {
		for _, source := range expected {
			if strings.EqualFold(filepath.ToSlash(chunk.FileName), filepath.ToSlash(source)) {
				hits[strings.ToLower(source)] = struct{}{}
			}
		}
	}
	return float64(len(hits)) / float64(len(expected))
}

func evidenceRecall(expected []string, chunks []ChunkResult) float64 {
	expected = compactStrings(expected)
	if len(expected) == 0 {
		return 1
	}
	haystack := normalizeText(joinChunkContent(chunks))
	hits := 0
	for _, evidence := range expected {
		if strings.Contains(haystack, normalizeText(evidence)) {
			hits++
		}
	}
	return float64(hits) / float64(len(expected))
}

func reciprocalRank(expected []string, chunks []ChunkResult) float64 {
	if len(expected) == 0 {
		return 1
	}
	for index, chunk := range chunks {
		for _, source := range expected {
			if strings.EqualFold(filepath.ToSlash(chunk.FileName), filepath.ToSlash(source)) {
				return 1 / float64(index+1)
			}
		}
	}
	return 0
}

func generateAnswer(ctx context.Context, cfg GenerationConfig, question string, chunks []ChunkResult) (string, string, string, error) {
	answer, err := generateAnswerOnce(ctx, cfg, question, chunks)
	if err == nil {
		return answer, cfg.Provider, cfg.Model, nil
	}
	if cfg.Fallback != nil && cfg.Fallback.Enabled && isAPIError(err) {
		fallback := GenerationConfig{
			Enabled:             true,
			Provider:            cfg.Fallback.Provider,
			BaseURL:             cfg.Fallback.BaseURL,
			Model:               cfg.Fallback.Model,
			APIKeyEnv:           cfg.Fallback.APIKeyEnv,
			TimeoutSeconds:      cfg.Fallback.TimeoutSeconds,
			RetryMaxAttempts:    cfg.Fallback.RetryMaxAttempts,
			RetryBackoffSeconds: cfg.Fallback.RetryBackoffSeconds,
			MaxContextBytes:     cfg.MaxContextBytes,
		}
		fmt.Printf("generation_failover from=%s model=%s to=%s model=%s reason=%s\n", cfg.Provider, cfg.Model, fallback.Provider, fallback.Model, truncateErrorBody(err.Error()))
		fallbackAnswer, fallbackErr := generateAnswerOnce(ctx, fallback, question, chunks)
		if fallbackErr == nil {
			return fallbackAnswer, fallback.Provider, fallback.Model, nil
		}
		return "", "", "", fmt.Errorf("API_ERROR: primary generation (%s): %v; fallback generation (%s): %v", cfg.Provider, err, fallback.Provider, fallbackErr)
	}
	return "", "", "", err
}

func generationContextPart(chunk ChunkResult) string {
	return fmt.Sprintf("[source=%s page=%d source_uri=%s chunk=%s]\n%s", chunk.FileName, chunk.PageNumber, chunk.SourceURI, chunk.ChunkID, chunk.Content)
}

func truncateUTF8(value string, maxBytes int) string {
	if maxBytes <= 0 {
		return ""
	}
	if len([]byte(value)) <= maxBytes {
		return value
	}
	var out strings.Builder
	out.Grow(maxBytes)
	for _, runeValue := range value {
		encoded := string(runeValue)
		if out.Len()+len([]byte(encoded)) > maxBytes {
			break
		}
		out.WriteString(encoded)
	}
	return out.String()
}

// boundedGenerationChunks keeps the full retrieved result available for
// retrieval scoring while projecting only a bounded evidence window to the
// text-only generator. MaaS GLM-5.2 rejects oversized prompts before it can
// return an answer, and native parent expansion can otherwise make a single
// result contain thousands of chunks.
func boundedGenerationChunks(chunks []ChunkResult, maxBytes int) ([]ChunkResult, int, bool) {
	if maxBytes <= 0 {
		maxBytes = defaultGenerationContextBytes
	}
	selected := make([]ChunkResult, 0, len(chunks))
	usedBytes := 0
	truncated := false
	for _, chunk := range chunks {
		part := generationContextPart(chunk)
		separatorBytes := 0
		if len(selected) > 0 {
			separatorBytes = 2 // strings.Join(contextParts, "\n\n")
		}
		partBytes := len([]byte(part))
		if usedBytes+separatorBytes+partBytes <= maxBytes {
			selected = append(selected, chunk)
			usedBytes += separatorBytes + partBytes
			continue
		}
		truncated = true
		if len(selected) == 0 {
			// Preserve at least the highest-ranked chunk when it is unusually
			// large, but never split a UTF-8 sequence.
			prefixBytes := len([]byte(generationContextPart(ChunkResult{
				FileName: chunk.FileName, PageNumber: chunk.PageNumber,
				SourceURI: chunk.SourceURI, ChunkID: chunk.ChunkID,
			})))
			clipped := chunk
			clipped.Content = truncateUTF8(chunk.Content, maxBytes-prefixBytes)
			clippedPart := generationContextPart(clipped)
			if len([]byte(clippedPart)) <= maxBytes {
				selected = append(selected, clipped)
				usedBytes = len([]byte(clippedPart))
			}
		}
		break
	}
	return selected, usedBytes, truncated
}

func generateAnswerOnce(ctx context.Context, cfg GenerationConfig, question string, chunks []ChunkResult) (string, error) {
	if cfg.BaseURL == "" || cfg.Model == "" {
		return "", errors.New("generation enabled but base_url or model is empty")
	}
	boundedChunks, _, _ := boundedGenerationChunks(chunks, cfg.MaxContextBytes)
	var contextParts []string
	for _, chunk := range boundedChunks {
		contextParts = append(contextParts, generationContextPart(chunk))
	}
	includePageImages := cfg.IncludePageImages == nil || *cfg.IncludePageImages
	userContent, err := generationUserContent(question, strings.Join(contextParts, "\n\n"), boundedChunks, includePageImages)
	if err != nil {
		return "", err
	}
	systemPrompt := strings.TrimSpace(cfg.SystemPrompt)
	if systemPrompt == "" {
		systemPrompt = "Answer only from the supplied evidence. If the evidence is insufficient, say so. Cite the source filename and PDF page for every material claim; when source_uri includes provenance such as SHA-256, retain it in the citation, and name the visible section when possible."
	}
	payload := map[string]any{
		"model": cfg.Model,
		"messages": []map[string]any{
			{"role": "system", "content": systemPrompt},
			{"role": "user", "content": userContent},
		},
		"temperature": 0,
		"stream":      false,
		"max_tokens":  1024,
	}
	if thinking := strings.ToLower(strings.TrimSpace(cfg.Thinking)); thinking != "" {
		if thinking != "enabled" && thinking != "disabled" {
			return "", fmt.Errorf("unsupported thinking mode %q; want enabled or disabled", cfg.Thinking)
		}
		payload["thinking"] = map[string]string{"type": thinking}
	}
	var response struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	client := &http.Client{
		Timeout:   time.Duration(cfg.TimeoutSeconds) * time.Second,
		Transport: newHTTPTransport(isDirectProvider(cfg.Provider)),
	}
	policy := requestRetryPolicy{
		MaxAttempts: cfg.RetryMaxAttempts,
		BaseDelay:   time.Duration(cfg.RetryBackoffSeconds * float64(time.Second)),
		MaxDelay:    60 * time.Second,
	}
	if err := postOpenAIJSONWithRetry(ctx, client, cfg.BaseURL, "/chat/completions", cfg.APIKeyEnv, payload, &response, policy); err != nil {
		return "", err
	}
	if len(response.Choices) == 0 {
		return "", errors.New("chat completion returned no choices")
	}
	return response.Choices[0].Message.Content, nil
}

func generationUserContent(question, evidence string, chunks []ChunkResult, includePageImages bool) (any, error) {
	text := "Question:\n" + question + "\n\nEvidence:\n" + evidence
	if !includePageImages {
		return text, nil
	}
	parts := []map[string]any{{"type": "text", "text": text}}
	imageCount := 0
	seen := make(map[string]struct{})
	for _, chunk := range chunks {
		path := strings.TrimSpace(chunk.PageImageFileID)
		if path == "" {
			continue
		}
		absolutePath, err := filepath.Abs(path)
		if err != nil {
			return nil, fmt.Errorf("resolve page image for %s page %d: %w", chunk.FileName, chunk.PageNumber, err)
		}
		if _, ok := seen[absolutePath]; ok {
			continue
		}
		raw, err := os.ReadFile(absolutePath)
		if err != nil {
			return nil, fmt.Errorf("read page image for %s page %d: %w", chunk.FileName, chunk.PageNumber, err)
		}
		mimeType := imageMIMEType(absolutePath, raw)
		parts = append(parts,
			map[string]any{"type": "text", "text": fmt.Sprintf("Page image for [source=%s page=%d]:", chunk.FileName, chunk.PageNumber)},
			map[string]any{"type": "image_url", "image_url": map[string]any{"url": "data:" + mimeType + ";base64," + base64.StdEncoding.EncodeToString(raw)}},
		)
		seen[absolutePath] = struct{}{}
		imageCount++
	}
	if imageCount == 0 {
		return text, nil
	}
	return parts, nil
}

func imageMIMEType(path string, raw []byte) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".png":
		return "image/png"
	case ".webp":
		return "image/webp"
	case ".gif":
		return "image/gif"
	default:
		return http.DetectContentType(raw)
	}
}

func postOpenAIJSON(ctx context.Context, client *http.Client, baseURL, path, apiKeyEnv string, payload, output any) error {
	return postOpenAIJSONWithRetry(ctx, client, baseURL, path, apiKeyEnv, payload, output, requestRetryPolicy{MaxAttempts: 1})
}

func postOpenAIJSONWithRetry(ctx context.Context, client *http.Client, baseURL, path, apiKeyEnv string, payload, output any, policy requestRetryPolicy) error {
	if policy.MaxAttempts <= 0 {
		policy.MaxAttempts = 1
	}
	if policy.BaseDelay <= 0 {
		policy.BaseDelay = time.Second
	}
	if policy.MaxDelay <= 0 {
		policy.MaxDelay = 60 * time.Second
	}
	var lastErr error
	for attempt := 1; attempt <= policy.MaxAttempts; attempt++ {
		lastErr = postOpenAIJSONOnce(ctx, client, baseURL, path, apiKeyEnv, payload, output)
		if lastErr == nil {
			return nil
		}
		if attempt == policy.MaxAttempts || !retryableAPIError(lastErr) {
			return lastErr
		}
		if httpErr := asAPIHTTPError(lastErr); httpErr != nil && httpErr.StatusCode == http.StatusMethodNotAllowed {
			// The observed 405 is produced by an upstream security gateway. Do
			// not reuse an idle connection after that response.
			client.CloseIdleConnections()
		}
		delay := retryDelay(policy, attempt, lastErr)
		fmt.Printf("api_retry path=%s attempt=%d/%d reason=%s wait=%s\n", path, attempt+1, policy.MaxAttempts, retryReason(lastErr), delay)
		if err := waitContext(ctx, delay); err != nil {
			return fmt.Errorf("API_ERROR: retry wait: %w", err)
		}
	}
	return lastErr
}

func postOpenAIJSONOnce(ctx context.Context, client *http.Client, baseURL, path, apiKeyEnv string, payload, output any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(baseURL, "/")+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", "moi-benchmark-rag/1.0")
	if apiKeyEnv != "" {
		apiKey := strings.TrimSpace(os.Getenv(apiKeyEnv))
		if apiKey == "" {
			return fmt.Errorf("API_ERROR: API key environment variable %s is not set", apiKeyEnv)
		}
		request.Header.Set("Authorization", "Bearer "+apiKey)
	}
	response, err := client.Do(request)
	if err != nil {
		return &apiRequestError{URL: request.URL.String(), Err: err}
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxAPIResponseBytes))
	if err != nil {
		return err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return &apiHTTPError{
			StatusCode: response.StatusCode,
			Body:       truncateErrorBody(string(raw)),
			RetryAfter: parseRetryAfter(response.Header.Get("Retry-After")),
		}
	}
	if err := json.Unmarshal(raw, output); err != nil {
		return fmt.Errorf("API_ERROR: decode HTTP response: %w", err)
	}
	return nil
}

func asAPIHTTPError(err error) *apiHTTPError {
	var target *apiHTTPError
	if errors.As(err, &target) {
		return target
	}
	return nil
}

func retryableAPIError(err error) bool {
	if httpErr := asAPIHTTPError(err); httpErr != nil {
		switch httpErr.StatusCode {
		case http.StatusMethodNotAllowed, http.StatusRequestTimeout, http.StatusTooEarly, http.StatusTooManyRequests:
			return true
		default:
			return httpErr.StatusCode >= 500
		}
	}
	var requestErr *apiRequestError
	return errors.As(err, &requestErr)
}

func retryReason(err error) string {
	if httpErr := asAPIHTTPError(err); httpErr != nil {
		return fmt.Sprintf("HTTP_%d", httpErr.StatusCode)
	}
	return "network_error"
}

func retryDelay(policy requestRetryPolicy, failedAttempt int, err error) time.Duration {
	delay := policy.BaseDelay
	if httpErr := asAPIHTTPError(err); httpErr != nil && httpErr.StatusCode == http.StatusMethodNotAllowed && policy.MethodNotAllowedMinDelay > delay {
		delay = policy.MethodNotAllowedMinDelay
	}
	for index := 1; index < failedAttempt; index++ {
		if delay >= policy.MaxDelay/2 {
			delay = policy.MaxDelay
			break
		}
		delay *= 2
	}
	if httpErr := asAPIHTTPError(err); httpErr != nil && httpErr.RetryAfter > delay {
		delay = httpErr.RetryAfter
	}
	if delay > policy.MaxDelay {
		return policy.MaxDelay
	}
	return delay
}

func parseRetryAfter(value string) time.Duration {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0
	}
	if seconds, err := strconv.Atoi(value); err == nil && seconds >= 0 {
		return time.Duration(seconds) * time.Second
	}
	if when, err := http.ParseTime(value); err == nil {
		if delay := time.Until(when); delay > 0 {
			return delay
		}
	}
	return 0
}

func waitContext(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func truncateErrorBody(value string) string {
	value = strings.TrimSpace(value)
	const maxErrorBody = 8 * 1024
	if len(value) <= maxErrorBody {
		return value
	}
	return value[:maxErrorBody] + "…"
}

func isAPIError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "api_error") || strings.Contains(message, "http ") ||
		strings.Contains(message, "api key environment variable")
}

func summarize(results []RunResult) Summary {
	summary := Summary{
		SchemaVersion: schemaVersion,
		CreatedAt:     time.Now().UTC().Format(time.RFC3339),
		Attempts:      len(results),
	}
	var sourceScores, evidenceScores, reciprocalRanks, retrievalLatencies, answerabilityScores, answerScores, generationLatencies []float64
	stageLatencies := make(map[string][]float64)
	for _, result := range results {
		retrievalLatencies = append(retrievalLatencies, result.RetrievalLatencyMS)
		for stage, latency := range result.StageLatencyMS {
			stageLatencies[stage] = append(stageLatencies[stage], latency)
		}
		if result.Status != "ok" {
			continue
		}
		summary.SuccessfulAttempts++
		sourceScores = append(sourceScores, result.Metrics.SourceRecall)
		evidenceScores = append(evidenceScores, result.Metrics.EvidenceRecall)
		reciprocalRanks = append(reciprocalRanks, result.Metrics.ReciprocalRank)
		if result.Metrics.AnswerabilityAccuracy != nil {
			answerabilityScores = append(answerabilityScores, *result.Metrics.AnswerabilityAccuracy)
		}
		if result.Metrics.AnswerKeywordScore != nil {
			answerScores = append(answerScores, *result.Metrics.AnswerKeywordScore)
		}
		if result.GenerationLatency != nil {
			generationLatencies = append(generationLatencies, *result.GenerationLatency)
		}
	}
	summary.MeanSourceRecall = mean(sourceScores)
	summary.MeanEvidenceRecall = mean(evidenceScores)
	summary.MeanReciprocalRank = mean(reciprocalRanks)
	summary.RetrievalLatencyMeanMS = mean(retrievalLatencies)
	summary.RetrievalLatencyP50MS = percentile(retrievalLatencies, 0.50)
	summary.RetrievalLatencyP95MS = percentile(retrievalLatencies, 0.95)
	summary.StageLatencyMeanMS = make(map[string]float64, len(stageLatencies))
	summary.StageLatencyP95MS = make(map[string]float64, len(stageLatencies))
	for stage, latencies := range stageLatencies {
		summary.StageLatencyMeanMS[stage] = mean(latencies)
		summary.StageLatencyP95MS[stage] = percentile(latencies, 0.95)
	}
	if len(answerabilityScores) > 0 {
		value := mean(answerabilityScores)
		summary.MeanAnswerabilityAccuracy = &value
	}
	if len(answerScores) > 0 {
		value := mean(answerScores)
		summary.MeanAnswerKeywordRecall = &value
	}
	if len(generationLatencies) > 0 {
		meanValue := mean(generationLatencies)
		p95 := percentile(generationLatencies, 0.95)
		summary.GenerationLatencyMeanMS = &meanValue
		summary.GenerationLatencyP95MS = &p95
	}
	return summary
}

func writeReport(path string, summary Summary) error {
	lines := []string{
		"# Local MatrixFlow product RAG benchmark",
		"",
		"This run executes MatrixFlow's native `SearchRAGChunks` implementation against a local MatrixOne vector table.",
		"",
		fmt.Sprintf("- Attempts: %d", summary.Attempts),
		fmt.Sprintf("- Successful attempts: %d", summary.SuccessfulAttempts),
		fmt.Sprintf("- Mean source recall: %.4f", summary.MeanSourceRecall),
		fmt.Sprintf("- Mean evidence recall: %.4f", summary.MeanEvidenceRecall),
		fmt.Sprintf("- Mean reciprocal rank: %.4f", summary.MeanReciprocalRank),
		fmt.Sprintf("- Retrieval latency mean: %.2f ms", summary.RetrievalLatencyMeanMS),
		fmt.Sprintf("- Retrieval latency P50: %.2f ms", summary.RetrievalLatencyP50MS),
		fmt.Sprintf("- Retrieval latency P95: %.2f ms", summary.RetrievalLatencyP95MS),
	}
	stages := make([]string, 0, len(summary.StageLatencyMeanMS))
	for stage := range summary.StageLatencyMeanMS {
		stages = append(stages, stage)
	}
	sort.Strings(stages)
	for _, stage := range stages {
		lines = append(lines, fmt.Sprintf("- %s latency mean / P95: %.2f / %.2f ms",
			stage, summary.StageLatencyMeanMS[stage], summary.StageLatencyP95MS[stage]))
	}
	if summary.MeanAnswerabilityAccuracy != nil {
		lines = append(lines, fmt.Sprintf("- Mean answerability accuracy: %.4f", *summary.MeanAnswerabilityAccuracy))
	}
	if summary.MeanAnswerKeywordRecall != nil {
		lines = append(lines, fmt.Sprintf("- Mean answer keyword recall: %.4f", *summary.MeanAnswerKeywordRecall))
	}
	return writeFile(path, strings.Join(lines, "\n")+"\n")
}

func keywordRecall(text string, expected []string) *float64 {
	expected = compactStrings(expected)
	if len(expected) == 0 {
		return nil
	}
	normalized := normalizeText(text)
	hits := 0
	for _, item := range expected {
		if strings.Contains(normalized, normalizeText(item)) {
			hits++
		}
	}
	value := float64(hits) / float64(len(expected))
	return &value
}

func normalizeText(value string) string {
	return strings.ToLower(strings.Join(strings.Fields(value), ""))
}

func joinChunkContent(chunks []ChunkResult) string {
	parts := make([]string, 0, len(chunks))
	for _, chunk := range chunks {
		parts = append(parts, chunk.Content)
	}
	return strings.Join(parts, "\n")
}

func compactStrings(values []string) []string {
	var out []string
	seen := map[string]struct{}{}
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		key := strings.ToLower(value)
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out = append(out, value)
	}
	return out
}

func mean(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	var total float64
	for _, value := range values {
		total += value
	}
	return total / float64(len(values))
}

func percentile(values []float64, fraction float64) float64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	index := int(math.Ceil(float64(len(sorted))*fraction)) - 1
	if index < 0 {
		index = 0
	}
	return sorted[index]
}

func stableID(prefix, value string) string {
	digest := sha256.Sum256([]byte(value))
	return prefix + "_" + hex.EncodeToString(digest[:8])
}

func sha256Text(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func writeJSON(path string, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return writeFile(path, string(raw)+"\n")
}

func writeJSONL(path string, values []RunResult) error {
	var builder strings.Builder
	for _, value := range values {
		raw, err := json.Marshal(value)
		if err != nil {
			return err
		}
		builder.Write(raw)
		builder.WriteByte('\n')
	}
	return writeFile(path, builder.String())
}

func appendJSONL(path string, value RunResult) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer file.Close()
	_, err = file.Write(append(raw, '\n'))
	return err
}

func writeFile(path, content string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(content), 0o644)
}
