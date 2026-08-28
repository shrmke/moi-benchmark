package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/matrixflow/moi-core/agent-tools/knowledge"
	knowledgeservice "github.com/matrixflow/moi-core/agent-tools/knowledge/service"
	"github.com/matrixflow/moi-core/workers/go-worker/pkg/workitems"
)

func TestLoadConfigAppliesTaaSDefaults(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	raw := `{
		"matrixone": {
			"dsn": "root:111@tcp(127.0.0.1:6001)/",
			"database": "benchmark",
			"vector_table": "embedding_results"
		},
		"embedding": {
			"mode": "taas",
			"model": "qwen3-embedding-0.6b",
			"dimension": 1024
		},
		"generation": {
			"enabled": true,
			"provider": "taas",
			"model": "qwen3.6-flash"
		}
	}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, err := loadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Embedding.BaseURL != taasAPIBaseURL || cfg.Generation.BaseURL != taasAPIBaseURL {
		t.Fatalf("TaaS base URLs = embedding %q, generation %q", cfg.Embedding.BaseURL, cfg.Generation.BaseURL)
	}
	if cfg.Embedding.APIKeyEnv != taasAPIKeyEnvName || cfg.Generation.APIKeyEnv != taasAPIKeyEnvName {
		t.Fatalf("TaaS key envs = embedding %q, generation %q", cfg.Embedding.APIKeyEnv, cfg.Generation.APIKeyEnv)
	}
	if cfg.Overlap != 64 || cfg.EmbeddingBatchSize != 64 || cfg.EmbeddingConcurrency != 1 || cfg.InsertBatchSize != 50 {
		t.Fatalf("production defaults = overlap %d, embedding batch %d, embedding concurrency %d, insert batch %d", cfg.Overlap, cfg.EmbeddingBatchSize, cfg.EmbeddingConcurrency, cfg.InsertBatchSize)
	}
	if _, err := newEmbedder(cfg.Embedding); err != nil {
		t.Fatalf("newEmbedder() rejected TaaS mode: %v", err)
	}
}

func TestLoadConfigAppliesHuaweiMaaSDefaults(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	raw := `{
		"matrixone": {
			"dsn": "root:111@tcp(127.0.0.1:6001)/",
			"database": "benchmark",
			"vector_table": "embedding_results"
		},
		"embedding": {
			"mode": "maas",
			"model": "bge-m3",
			"dimension": 1024
		},
		"generation": {
			"enabled": true,
			"provider": "maas",
			"model": "qwen3-30b-a3b"
		}
	}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, err := loadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Embedding.BaseURL != maasAPIBaseURL || cfg.Generation.BaseURL != maasAPIBaseURL {
		t.Fatalf("MaaS base URLs = embedding %q, generation %q", cfg.Embedding.BaseURL, cfg.Generation.BaseURL)
	}
	if cfg.Embedding.APIKeyEnv != maasAPIKeyEnvName || cfg.Generation.APIKeyEnv != maasAPIKeyEnvName {
		t.Fatalf("MaaS key envs = embedding %q, generation %q", cfg.Embedding.APIKeyEnv, cfg.Generation.APIKeyEnv)
	}
	if _, err := newEmbedder(cfg.Embedding); err != nil {
		t.Fatalf("newEmbedder() rejected MaaS mode: %v", err)
	}
}

func TestLoadConfigPreservesOpenAIEmbeddingBatchSize(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	raw := `{
		"matrixone": {
			"dsn": "root:111@tcp(127.0.0.1:6001)/",
			"database": "benchmark",
			"vector_table": "embedding_results"
		},
		"embedding_batch_size": 128,
		"embedding": {
			"mode": "openai",
			"base_url": "http://127.0.0.1:8081/v1",
			"model": "BAAI/bge-m3",
			"dimension": 1024
		}
	}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, err := loadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.EmbeddingBatchSize != 128 {
		t.Fatalf("OpenAI embedding batch = %d, want 128", cfg.EmbeddingBatchSize)
	}
}

func TestCloudProviderTransportBypassesEnvironmentProxy(t *testing.T) {
	direct := newHTTPTransport(true)
	if direct.Proxy != nil {
		t.Fatal("direct cloud provider transport still has an environment proxy")
	}
	if !isDirectProvider("taas") || !isDirectProvider("maas") || isDirectProvider("openai") {
		t.Fatal("direct provider classification is incorrect")
	}
	defaultRoute := newHTTPTransport(false)
	if defaultRoute.Proxy == nil {
		t.Fatal("default provider transport lost environment proxy support")
	}
}

func TestPostOpenAIJSONUsesBearerToken(t *testing.T) {
	t.Setenv(taasAPIKeyEnvName, "test-taas-key")
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if got := request.URL.Path; got != "/embeddings" {
			t.Errorf("request path = %q, want /embeddings", got)
		}
		if got := request.Header.Get("Authorization"); got != "Bearer test-taas-key" {
			t.Errorf("Authorization = %q", got)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"data":[]}`))
	}))
	defer server.Close()

	var response map[string]any
	err := postOpenAIJSON(
		context.Background(),
		server.Client(),
		server.URL,
		"/embeddings",
		taasAPIKeyEnvName,
		map[string]any{"model": "qwen3-embedding-0.6b", "input": []string{"hello"}},
		&response,
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := response["data"].([]any); !ok {
		encoded, _ := json.Marshal(response)
		t.Fatalf("response did not contain data array: %s", encoded)
	}
}

func TestPostOpenAIJSONRejectsMissingConfiguredAPIKey(t *testing.T) {
	t.Setenv(taasAPIKeyEnvName, "")
	err := postOpenAIJSON(
		context.Background(),
		http.DefaultClient,
		"https://example.invalid",
		"/embeddings",
		taasAPIKeyEnvName,
		map[string]any{},
		&map[string]any{},
	)
	if err == nil || !strings.Contains(err.Error(), taasAPIKeyEnvName) {
		t.Fatalf("missing-key error = %v", err)
	}
}

func TestPostOpenAIJSONRetriesTransient405(t *testing.T) {
	t.Setenv(taasAPIKeyEnvName, "test-taas-key")
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if calls.Add(1) == 1 {
			writer.Header().Set("Retry-After", "0")
			writer.WriteHeader(http.StatusMethodNotAllowed)
			_, _ = writer.Write([]byte("temporary WAF response"))
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"data":[]}`))
	}))
	defer server.Close()

	var response map[string]any
	err := postOpenAIJSONWithRetry(
		context.Background(),
		server.Client(),
		server.URL,
		"/embeddings",
		taasAPIKeyEnvName,
		map[string]any{"model": "bge-m3", "input": []string{"hello"}},
		&response,
		requestRetryPolicy{MaxAttempts: 2, BaseDelay: time.Millisecond},
	)
	if err != nil {
		t.Fatal(err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("request attempts = %d, want 2", got)
	}
}

func TestGenerateAnswerSendsConfiguredThinkingMode(t *testing.T) {
	t.Setenv("DEEPSEEK_API_KEY_NEW", "test-deepseek-key")
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		var payload map[string]any
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			t.Errorf("decode request: %v", err)
		}
		messages, ok := payload["messages"].([]any)
		if !ok || len(messages) == 0 {
			t.Fatalf("messages payload = %#v", payload["messages"])
		}
		systemMessage, ok := messages[0].(map[string]any)
		if !ok || systemMessage["role"] != "system" || systemMessage["content"] != "shared benchmark prompt" {
			t.Errorf("system message = %#v", messages[0])
		}
		thinking, ok := payload["thinking"].(map[string]any)
		if !ok || thinking["type"] != "disabled" {
			t.Errorf("thinking payload = %#v", payload["thinking"])
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"choices":[{"message":{"content":"answer"}}]}`))
	}))
	defer server.Close()

	answer, provider, model, err := generateAnswer(
		context.Background(),
		GenerationConfig{
			Enabled:          true,
			Provider:         "deepseek-official",
			BaseURL:          server.URL,
			Model:            "deepseek-v4-flash",
			APIKeyEnv:        "DEEPSEEK_API_KEY_NEW",
			TimeoutSeconds:   2,
			RetryMaxAttempts: 1,
			Thinking:         "disabled",
			SystemPrompt:     "shared benchmark prompt",
		},
		"question",
		[]ChunkResult{{FileName: "doc.txt", ChunkID: "chunk-1", Content: "evidence"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if answer != "answer" || provider != "deepseek-official" || model != "deepseek-v4-flash" {
		t.Fatalf("result = (%q, %q, %q)", answer, provider, model)
	}
}

func TestGenerateAnswerFallsBackToQianfanAfterTaaSTimeout(t *testing.T) {
	t.Setenv(taasAPIKeyEnvName, "test-taas-key")
	t.Setenv("QIANFAN_API_KEY", "test-qianfan-key")
	primary := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.WriteHeader(http.StatusGatewayTimeout)
		_, _ = writer.Write([]byte("gateway timeout"))
	}))
	defer primary.Close()
	fallback := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if got := request.Header.Get("Authorization"); got != "Bearer test-qianfan-key" {
			t.Errorf("Qianfan Authorization = %q", got)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"choices":[{"message":{"content":"fallback answer"}}]}`))
	}))
	defer fallback.Close()

	answer, provider, model, err := generateAnswer(
		context.Background(),
		GenerationConfig{
			Enabled:          true,
			Provider:         "taas",
			BaseURL:          primary.URL,
			Model:            "qwen3.6-flash",
			APIKeyEnv:        taasAPIKeyEnvName,
			TimeoutSeconds:   2,
			RetryMaxAttempts: 1,
			Fallback: &GenerationFallbackConfig{
				Enabled:             true,
				Provider:            "qianfan",
				BaseURL:             fallback.URL,
				Model:               "deepseek-v4-flash",
				APIKeyEnv:           "QIANFAN_API_KEY",
				TimeoutSeconds:      2,
				RetryMaxAttempts:    1,
				RetryBackoffSeconds: 0,
			},
		},
		"question",
		[]ChunkResult{{FileName: "doc.pdf", ChunkID: "chunk-1", Content: "evidence"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if answer != "fallback answer" || provider != "qianfan" || model != "deepseek-v4-flash" {
		t.Fatalf("fallback result = (%q, %q, %q)", answer, provider, model)
	}
}

func TestGenerationUserContentIncludesPageImageDataURL(t *testing.T) {
	imagePath := filepath.Join(t.TempDir(), "page.jpg")
	jpeg := []byte{0xff, 0xd8, 0xff, 0xd9}
	if err := os.WriteFile(imagePath, jpeg, 0o644); err != nil {
		t.Fatal(err)
	}
	content, err := generationUserContent("What is shown?", "OCR evidence", []ChunkResult{{
		FileName: "doc.pdf", PageNumber: 4, PageImageFileID: imagePath,
	}}, true)
	if err != nil {
		t.Fatal(err)
	}
	parts, ok := content.([]map[string]any)
	if !ok || len(parts) != 3 {
		t.Fatalf("multimodal content = %#v", content)
	}
	imagePart, ok := parts[2]["image_url"].(map[string]any)
	if !ok || imagePart["url"] != "data:image/jpeg;base64,/9j/2Q==" {
		t.Fatalf("image part = %#v", parts[2])
	}
}

func TestBoundedGenerationChunksCapsEvidenceWithoutChangingRetrievalInput(t *testing.T) {
	chunks := []ChunkResult{
		{Rank: 1, FileName: "first.txt", ChunkID: "first", Content: strings.Repeat("甲", 80)},
		{Rank: 2, FileName: "second.txt", ChunkID: "second", Content: strings.Repeat("乙", 80)},
	}
	selected, usedBytes, truncated := boundedGenerationChunks(chunks, 180)
	if !truncated {
		t.Fatal("bounded context did not report truncation")
	}
	if len(selected) != 1 {
		t.Fatalf("selected chunks = %d, want 1", len(selected))
	}
	if usedBytes > 180 {
		t.Fatalf("used bytes = %d, want <= 180", usedBytes)
	}
	if chunks[0].Content != strings.Repeat("甲", 80) || chunks[1].Content != strings.Repeat("乙", 80) {
		t.Fatal("bounded context mutated the retrieval chunks")
	}
}

func TestProductEmbeddingContractTruncatesAndBatches(t *testing.T) {
	docs := make([]workitems.Document, 0, 65)
	for i := 0; i < 65; i++ {
		docs = append(docs, workitems.Document{
			Content:  strings.Repeat("界", 5000),
			Metadata: map[string]interface{}{"file_id": "file-1", "chunk_index": i},
		})
	}
	indexes, inputs := workitems.CollectEmbeddingInputsForLocalRAG(docs)
	if len(indexes) != len(docs) || len(inputs) != len(docs) {
		t.Fatalf("embedding inputs = %d/%d, want %d/%d", len(indexes), len(inputs), len(docs), len(docs))
	}
	if got := len(inputs[0]); got > 8192 {
		t.Fatalf("embedding input bytes = %d, want <= 8192", got)
	}
	batches := workitems.SplitEmbeddingInputsForLocalRAG(inputs)
	if len(batches) != 3 || len(batches[0].Inputs) != 32 || len(batches[1].Inputs) != 32 || len(batches[2].Inputs) != 1 {
		t.Fatalf("embedding batches = %#v, want 32+32+1 due to 256 KiB cap", batches)
	}
}

func TestLocalTaaSEmbeddingBatchSizePreservesByteLimit(t *testing.T) {
	inputs := make([]string, 0, 1025)
	for i := 0; i < 1025; i++ {
		inputs = append(inputs, fmt.Sprintf("item-%d", i))
	}
	batches := splitEmbeddingInputsForLocalRAG(inputs, 1024)
	if len(batches) != 2 || len(batches[0].Inputs) != 1024 || len(batches[1].Inputs) != 1 {
		t.Fatalf("batches = %#v, want 1024+1", batches)
	}
	for _, batch := range batches {
		if batch.Bytes > 256*1024 {
			t.Fatalf("batch bytes = %d, want <= 256 KiB", batch.Bytes)
		}
	}
}

func TestConcurrentEmbeddingBatchesConsumeInOrder(t *testing.T) {
	batches := make([]workitems.LocalRAGEmbeddingBatch, 4)
	for index := range batches {
		batches[index] = workitems.LocalRAGEmbeddingBatch{Start: index, End: index + 1, Inputs: []string{fmt.Sprint(index)}}
	}
	var active atomic.Int32
	var maximum atomic.Int32
	var mu sync.Mutex
	consumed := make([]int, 0, len(batches))

	err := processEmbeddingBatchesConcurrently(
		context.Background(),
		batches,
		3,
		func(_ context.Context, batchIndex int, _ workitems.LocalRAGEmbeddingBatch) ([][]float64, error) {
			current := active.Add(1)
			for {
				previous := maximum.Load()
				if current <= previous || maximum.CompareAndSwap(previous, current) {
					break
				}
			}
			defer active.Add(-1)
			time.Sleep(time.Duration(len(batches)-batchIndex) * 5 * time.Millisecond)
			return [][]float64{{float64(batchIndex)}}, nil
		},
		func(batchIndex int, _ workitems.LocalRAGEmbeddingBatch, _ [][]float64) error {
			mu.Lock()
			defer mu.Unlock()
			consumed = append(consumed, batchIndex)
			return nil
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if maximum.Load() < 2 {
		t.Fatalf("maximum embedding concurrency = %d, want at least 2", maximum.Load())
	}
	if got := fmt.Sprint(consumed); got != "[0 1 2 3]" {
		t.Fatalf("consumed batches = %s, want [0 1 2 3]", got)
	}
}

func TestLoadIngestResumeProgressAcceptsCommittedBoundary(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ingest-progress.json")
	raw := `{
		"stage":"writing",
		"parsed_documents":205978,
		"expanded_entries":311644,
		"embedded_entries":60617,
		"committed_entries":60617,
		"total_entries":311644,
		"batch_end":60617
	}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	resumeFrom, err := loadIngestResumeProgress(path, 205978, 311644, 311644)
	if err != nil {
		t.Fatal(err)
	}
	if resumeFrom != 60617 {
		t.Fatalf("resume offset = %d, want 60617", resumeFrom)
	}
}

func TestLoadIngestResumeProgressRejectsUncommittedBoundary(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ingest-progress.json")
	raw := `{
		"stage":"writing",
		"parsed_documents":2,
		"expanded_entries":4,
		"embedded_entries":3,
		"committed_entries":2,
		"total_entries":4,
		"batch_end":3
	}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadIngestResumeProgress(path, 2, 4, 4); err == nil || !strings.Contains(err.Error(), "committed batch boundary") {
		t.Fatalf("uncommitted resume error = %v", err)
	}
}

func TestAllocateRunDirNeverReusesAnExistingDirectory(t *testing.T) {
	root := filepath.Join(t.TempDir(), "runs", "mock")
	now := time.Date(2026, 7, 31, 12, 34, 56, 789000000, time.Local)
	first, err := allocateRunDir(root, now)
	if err != nil {
		t.Fatal(err)
	}
	second, err := allocateRunDir(root, now)
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatalf("run directories were reused: %s", first)
	}
	if filepath.Base(first) != "20260731-123456.789" {
		t.Fatalf("first run directory = %s", first)
	}
	if filepath.Base(second) != "20260731-123456.789-01" {
		t.Fatalf("second run directory = %s", second)
	}
	for _, path := range []string{first, second} {
		info, statErr := os.Stat(path)
		if statErr != nil || !info.IsDir() {
			t.Fatalf("run directory %s was not created", path)
		}
	}
}

func TestForceRebuildsVectorTableWhenEmbeddingDimensionChanges(t *testing.T) {
	dsn := os.Getenv("MATRIXONE_INTEGRATION_DSN")
	if dsn == "" {
		t.Skip("set MATRIXONE_INTEGRATION_DSN to run MatrixOne integration tests")
	}
	table := fmt.Sprintf("embedding_dimension_test_%d", time.Now().UnixNano())
	cfg := Config{MatrixOne: MatrixOneConfig{
		DSN: dsn, Database: "matrixflow_rag_benchmark", VectorTable: table,
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	db, err := openBenchmarkDB(ctx, cfg, 256, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = openBenchmarkDB(ctx, cfg, 1024, true)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	defer db.ExecContext(context.Background(), "DROP TABLE IF EXISTS `"+table+"`")

	var tableName, createSQL string
	if err := db.QueryRowContext(ctx, "SHOW CREATE TABLE `"+table+"`").Scan(&tableName, &createSQL); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.ToLower(createSQL), "vecf64(1024)") {
		t.Fatalf("force kept the old vector dimension; SHOW CREATE TABLE = %s", createSQL)
	}
}

func TestScoreCaseSeparatesSourceAndEvidenceRecall(t *testing.T) {
	answerable := true
	item := QuestionCase{
		RelevantDocuments:  []string{"guide.md", "policy.md"},
		RelevantEvidence:   []string{"MatrixFlow native retrieval", "missing evidence"},
		ExpectedAnswerable: &answerable,
	}
	chunks := []ChunkResult{
		{Rank: 1, FileName: "guide.md", Content: "MatrixFlow native retrieval uses hybrid search."},
		{Rank: 2, FileName: "other.md", Content: "Unrelated."},
	}
	metrics := scoreCase(item, chunks)
	if metrics.SourceRecall != 0.5 {
		t.Fatalf("source recall = %v, want 0.5", metrics.SourceRecall)
	}
	if metrics.EvidenceRecall != 0.5 {
		t.Fatalf("evidence recall = %v, want 0.5", metrics.EvidenceRecall)
	}
	if metrics.ReciprocalRank != 1 {
		t.Fatalf("MRR = %v, want 1", metrics.ReciprocalRank)
	}
	if metrics.RecallAtK["1"] != 0.5 {
		t.Fatalf("source recall@1 = %v, want 0.5", metrics.RecallAtK["1"])
	}
	if metrics.AnswerabilityAccuracy == nil || *metrics.AnswerabilityAccuracy != 1 {
		t.Fatalf("answerability accuracy = %v, want 1", metrics.AnswerabilityAccuracy)
	}
}

func TestScoreCasePenalizesUnexpectedEvidenceForUnanswerableCase(t *testing.T) {
	answerable := false
	metrics := scoreCase(QuestionCase{ExpectedAnswerable: &answerable}, []ChunkResult{{Content: "irrelevant evidence"}})
	if metrics.AnswerabilityAccuracy == nil || *metrics.AnswerabilityAccuracy != 0 {
		t.Fatalf("answerability accuracy = %v, want 0", metrics.AnswerabilityAccuracy)
	}
}

func TestClassifySQLStage(t *testing.T) {
	cases := map[string]string{
		"SHOW COLUMNS FROM t": "schema_inspection",
		"SHOW INDEX FROM t":   "schema_inspection",
		"SELECT * FROM t WHERE MATCH(content) AGAINST ('x')": "fulltext_search",
		"SELECT cosine_distance(embedding, '[1]') FROM t":    "vector_search",
		"SELECT l2_distance(embedding, '[1]') FROM t":        "vector_search",
		"SELECT * FROM t WHERE chunk_index BETWEEN 1 AND 3":  "evidence_expansion",
	}
	for query, want := range cases {
		if got := classifySQLStage(query); got != want {
			t.Errorf("classifySQLStage(%q) = %q, want %q", query, got, want)
		}
	}
}

func TestHashEmbeddingIsStableAndNormalized(t *testing.T) {
	client := hashEmbeddingClient{dimension: 64}
	first, err := client.CreateEmbedding(context.Background(), "", "", []string{"MatrixFlow 检索"})
	if err != nil {
		t.Fatal(err)
	}
	second, err := client.CreateEmbedding(context.Background(), "", "", []string{"MatrixFlow 检索"})
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != 1 || len(first[0]) != 64 {
		t.Fatalf("embedding shape = %dx%d, want 1x64", len(first), len(first[0]))
	}
	var norm float64
	for index, value := range first[0] {
		if value != second[0][index] {
			t.Fatal("hash embedding is not deterministic")
		}
		norm += value * value
	}
	if norm < 0.999 || norm > 1.001 {
		t.Fatalf("squared norm = %v, want approximately 1", norm)
	}
}

func TestEffectiveRAGFileIDsSkipsOnlyConfiguredExhaustiveScope(t *testing.T) {
	item := QuestionCase{FileIDs: []string{"file-a", "", "file-a", "file-b"}}
	got := effectiveRAGFileIDs(Config{}, item)
	if want := []string{"file-a", "file-b"}; fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("preserved file IDs = %v, want %v", got, want)
	}
	got = effectiveRAGFileIDs(Config{MatrixOne: MatrixOneConfig{SkipExhaustiveFileIDFilter: true}}, item)
	if got != nil {
		t.Fatalf("exhaustive file IDs = %v, want nil", got)
	}
}

func TestHarnessExecutesMatrixFlowNativeSearchRAGChunks(t *testing.T) {
	executor := &productRAGExecutor{}
	recorder := &timingRecorder{}
	recorder.reset()
	searcher := knowledgeservice.NewSearchRAGChunks(knowledgeservice.Deps{
		SQLExecutor: timedSQLExecutor{next: executor, recorder: recorder},
		Embedder: timedEmbeddingService{
			next:     hashEmbeddingClient{dimension: 3},
			recorder: recorder,
		},
		DefaultRetrieverConfig: knowledge.RetrieverConfig{
			EmbeddingModel: "test-embedding",
		},
	})
	response, err := searcher.Execute(context.Background(), knowledge.SearchRAGChunksRequest{
		Scope: knowledge.WorkspaceScope{
			WorkspaceID:    "local",
			DBName:         "benchmark",
			VectorTable:    "embedding_results",
			EmbeddingModel: "test-embedding",
		},
		Keywards: []string{"native retrieval"},
		MaxHits:  5,
	})
	if err != nil {
		t.Fatalf("SearchRAGChunks.Execute() error = %v", err)
	}
	if len(response.Chunks) != 1 {
		t.Fatalf("chunk count = %d, want 1", len(response.Chunks))
	}
	if response.Chunks[0].ChunkID != "chunk_native_1" {
		t.Fatalf("chunk id = %q, want chunk_native_1", response.Chunks[0].ChunkID)
	}
	if len(executor.queries) < 4 {
		t.Fatalf("query count = %d, want product column/fulltext/vector routes", len(executor.queries))
	}
	if !strings.Contains(executor.queries[2], "rag_fulltext_candidates") {
		t.Fatalf("third query is not MatrixFlow fulltext route: %s", executor.queries[2])
	}
	if !strings.Contains(executor.queries[3], "l2_distance") || !strings.Contains(executor.queries[3], "'vector_l2' AS route") {
		t.Fatalf("fourth query did not adapt to the existing L2 index: %s", executor.queries[3])
	}
	stageLatency := recorder.milliseconds()
	for _, stage := range []string{"schema_inspection", "fulltext_search", "vector_search", "embedding"} {
		if _, ok := stageLatency[stage]; !ok {
			t.Errorf("missing %s latency from native retrieval trace: %#v", stage, stageLatency)
		}
	}
}

type productRAGExecutor struct {
	queries []string
}

func (e *productRAGExecutor) ExecuteSQL(_ context.Context, _ string, query string) (*knowledge.SQLExecutionResult, error) {
	e.queries = append(e.queries, query)
	switch {
	case strings.HasPrefix(query, "SHOW COLUMNS"):
		return &knowledge.SQLExecutionResult{
			Columns: []string{"Field"},
			Rows: [][]any{
				{"file_id"}, {"index_version"}, {"level"}, {"content"}, {"meta"},
				{"embedding"}, {"chunk_index"}, {"disabled"},
			},
		}, nil
	case strings.HasPrefix(query, "SHOW INDEX"):
		return &knowledge.SQLExecutionResult{
			Columns: []string{"Column_name", "Index_type", "Index_params"},
			Rows:    [][]any{{"embedding", "ivfflat", `{"lists":"256","op_type":"vector_l2_ops"}`}},
		}, nil
	case strings.Contains(query, "rag_fulltext_candidates"):
		return &knowledge.SQLExecutionResult{
			Columns: []string{
				"route", "level", "content", "meta", "file_id", "markdown_file_id",
				"index_version", "chunk_index", "chunk_index_scope", "parent_index",
				"chunk_start", "chunk_end", "score",
			},
			Rows: [][]any{{
				"fulltext", "chunk", "MatrixFlow native retrieval evidence",
				`{"chunk_id":"chunk_native_1","source_file_name":"guide.md"}`,
				"file_guide", "", "1", 0, "", "", "", "", 1.0,
			}},
		}, nil
	case strings.Contains(query, "rag_vector_candidates"):
		return &knowledge.SQLExecutionResult{
			Columns: []string{
				"route", "level", "content", "meta", "file_id", "markdown_file_id",
				"index_version", "chunk_index", "chunk_index_scope", "parent_index",
				"chunk_start", "chunk_end", "score",
			},
		}, nil
	default:
		return &knowledge.SQLExecutionResult{}, nil
	}
}
