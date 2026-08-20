# Tech Pack: Go

> **Auto-loaded** when `.synaptory.yaml` specifies `go` in `tech-stack` or when `go.mod` is detected in the project root.

This tech pack provides Go-specific guidance for building production services. It assumes Go 1.22+ and follows the conventions established by the Go team and the broader Go community.

## Project Layout

Follow the standard Go project layout. Do not blindly copy the `golang-standards/project-layout` repo — it is not an official standard. Use what makes sense for your project size.

### Small Service (single binary)

```
myservice/
  cmd/
    myservice/
      main.go             # Entry point, wiring, graceful shutdown
  internal/
    server/
      server.go           # HTTP server setup, routes
      middleware.go        # Middleware chain
    handler/
      project.go          # HTTP handlers for /projects
      user.go             # HTTP handlers for /users
    service/
      project.go          # Business logic
      user.go
    repository/
      project.go          # Database access
      user.go
    model/
      project.go          # Domain types
      user.go
    config/
      config.go           # Configuration loading
  migrations/
    001_create_users.up.sql
    001_create_users.down.sql
  go.mod
  go.sum
  Makefile
  Dockerfile
```

### Large Service (multiple binaries, shared libraries)

```
myplatform/
  cmd/
    api-server/main.go
    worker/main.go
    cli/main.go
  internal/                # Private to this module
    domain/               # Core business types and interfaces
    api/                  # HTTP layer
    worker/               # Background job processing
    storage/              # Database implementations
  pkg/                    # Importable by other modules (use sparingly)
    pagination/
    validation/
  migrations/
  go.mod
```

### Key Layout Rules

- `cmd/` contains one directory per binary, each with a `main.go`
- `internal/` prevents external imports — use it for all application code
- `pkg/` is only for code genuinely intended for external consumption — most projects do not need it
- Never put application logic in `main.go` — it should only wire dependencies and start the server
- Group by domain responsibility (handler, service, repository), not by technical layer across domains

## Interface-Driven Design

**Define interfaces where they are consumed, not where they are implemented.**

```go
// internal/service/project.go
// The service defines what it needs from storage
type ProjectRepository interface {
    Get(ctx context.Context, id uuid.UUID) (*model.Project, error)
    List(ctx context.Context, filter ProjectFilter) ([]model.Project, error)
    Create(ctx context.Context, project *model.Project) error
    Update(ctx context.Context, project *model.Project) error
    Delete(ctx context.Context, id uuid.UUID) error
}

type ProjectService struct {
    repo   ProjectRepository
    logger *slog.Logger
}

func NewProjectService(repo ProjectRepository, logger *slog.Logger) *ProjectService {
    return &ProjectService{repo: repo, logger: logger}
}
```

```go
// internal/repository/project_postgres.go
// The implementation satisfies the interface implicitly
type PostgresProjectRepository struct {
    db *pgxpool.Pool
}

func NewPostgresProjectRepository(db *pgxpool.Pool) *PostgresProjectRepository {
    return &PostgresProjectRepository{db: db}
}

func (r *PostgresProjectRepository) Get(ctx context.Context, id uuid.UUID) (*model.Project, error) {
    // ...
}
```

### Interface Rules

1. **Keep interfaces small** — 1-3 methods is ideal. Split large interfaces into focused ones
2. **Accept interfaces, return structs** — function parameters use interfaces, return values use concrete types
3. **Do not create interfaces preemptively** — extract an interface when you have two implementations or need to mock in tests
4. **No `I` prefix** — Go interfaces are named by what they do: `Reader`, `ProjectRepository`, `Authenticator`

## Error Handling

### Sentinel Errors vs Wrapped Errors

```go
// internal/domain/errors.go
var (
    ErrNotFound      = errors.New("not found")
    ErrAlreadyExists = errors.New("already exists")
    ErrForbidden     = errors.New("forbidden")
    ErrValidation    = errors.New("validation error")
)

// Wrapping with context
func (r *PostgresProjectRepository) Get(ctx context.Context, id uuid.UUID) (*model.Project, error) {
    var project model.Project
    err := r.db.QueryRow(ctx, "SELECT ... WHERE id = $1", id).Scan(&project.ID, &project.Name)
    if err != nil {
        if errors.Is(err, pgx.ErrNoRows) {
            return nil, fmt.Errorf("project %s: %w", id, ErrNotFound)
        }
        return nil, fmt.Errorf("querying project %s: %w", id, err)
    }
    return &project, nil
}

// Checking errors
func (h *ProjectHandler) GetProject(w http.ResponseWriter, r *http.Request) {
    project, err := h.service.Get(r.Context(), id)
    if err != nil {
        if errors.Is(err, ErrNotFound) {
            writeJSON(w, http.StatusNotFound, errorResponse("Project not found"))
            return
        }
        writeJSON(w, http.StatusInternalServerError, errorResponse("Internal error"))
        return
    }
    writeJSON(w, http.StatusOK, project)
}
```

### Custom Error Types

```go
type ValidationError struct {
    Field   string
    Message string
}

func (e *ValidationError) Error() string {
    return fmt.Sprintf("validation: %s - %s", e.Field, e.Message)
}

// Check with errors.As
var validationErr *ValidationError
if errors.As(err, &validationErr) {
    writeJSON(w, http.StatusBadRequest, map[string]string{
        "field":   validationErr.Field,
        "message": validationErr.Message,
    })
}
```

### Error Handling Rules

1. **Always handle errors immediately** — never discard with `_`
2. **Wrap with context** — `fmt.Errorf("creating project: %w", err)` so the call chain is traceable
3. **Log at the top, wrap at the bottom** — handlers log, services/repos wrap and return
4. **Use `errors.Is` for sentinel errors**, `errors.As` for typed errors
5. **Never log and return the same error** — it gets logged twice

## Context Propagation

**Every function that does I/O or may be cancelled must accept `context.Context` as its first parameter.**

```go
func (s *ProjectService) Create(ctx context.Context, input CreateProjectInput) (*model.Project, error) {
    // Context flows through the entire call chain
    existing, err := s.repo.GetByName(ctx, input.Name)
    if err != nil && !errors.Is(err, ErrNotFound) {
        return nil, fmt.Errorf("checking existing project: %w", err)
    }
    if existing != nil {
        return nil, fmt.Errorf("project %q: %w", input.Name, ErrAlreadyExists)
    }
    // ...
}
```

### Context Rules

1. Never store context in a struct — pass it as the first parameter
2. Use `context.WithTimeout` for external calls (database, HTTP, gRPC)
3. Check `ctx.Err()` before expensive operations
4. Use `context.WithValue` sparingly — only for request-scoped data (request ID, user ID), never for function parameters

## Goroutine Lifecycle Management

### Preventing Goroutine Leaks

Every goroutine must have a clear shutdown path. Use `context.Context` or `done` channels.

```go
func (w *Worker) Start(ctx context.Context) {
    for {
        select {
        case <-ctx.Done():
            w.logger.Info("worker shutting down")
            return
        case job := <-w.jobs:
            if err := w.process(ctx, job); err != nil {
                w.logger.Error("processing job", "error", err, "job_id", job.ID)
            }
        }
    }
}
```

### errgroup for Parallel Work

```go
import "golang.org/x/sync/errgroup"

func (s *DashboardService) GetDashboard(ctx context.Context, userID uuid.UUID) (*Dashboard, error) {
    g, ctx := errgroup.WithContext(ctx)

    var metrics *Metrics
    var activity []Activity
    var projects []Project

    g.Go(func() error {
        var err error
        metrics, err = s.metricsService.Get(ctx, userID)
        return err
    })
    g.Go(func() error {
        var err error
        activity, err = s.activityService.List(ctx, userID)
        return err
    })
    g.Go(func() error {
        var err error
        projects, err = s.projectService.List(ctx, userID)
        return err
    })

    if err := g.Wait(); err != nil {
        return nil, fmt.Errorf("fetching dashboard: %w", err)
    }

    return &Dashboard{Metrics: metrics, Activity: activity, Projects: projects}, nil
}
```

## Graceful Shutdown

```go
func main() {
    ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
    defer stop()

    srv := &http.Server{
        Addr:    ":8080",
        Handler: router,
    }

    // Start server in background
    go func() {
        if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
            log.Fatalf("server error: %v", err)
        }
    }()

    // Wait for shutdown signal
    <-ctx.Done()
    log.Println("shutting down...")

    // Give active requests time to finish
    shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()

    if err := srv.Shutdown(shutdownCtx); err != nil {
        log.Fatalf("shutdown error: %v", err)
    }
    log.Println("server stopped")
}
```

## Dependency Injection Without Frameworks

Wire dependencies manually in `main.go`. Do not use DI frameworks (wire, dig, fx) unless the project is very large.

```go
func main() {
    cfg := config.Load()
    db := mustConnectDB(cfg.DatabaseURL)
    logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))

    // Wire the dependency graph
    projectRepo := repository.NewPostgresProjectRepository(db)
    userRepo := repository.NewPostgresUserRepository(db)

    projectService := service.NewProjectService(projectRepo, logger)
    userService := service.NewUserService(userRepo, logger)

    projectHandler := handler.NewProjectHandler(projectService)
    userHandler := handler.NewUserHandler(userService)

    router := server.NewRouter(projectHandler, userHandler, logger)
    // ...
}
```

## Structured Logging with slog

Go 1.21+ includes `log/slog` in the standard library. Use it instead of third-party loggers.

```go
logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
    Level: slog.LevelInfo,
}))

// Structured fields
logger.Info("project created",
    "project_id", project.ID,
    "owner_id", userID,
    "duration_ms", time.Since(start).Milliseconds(),
)

// With persistent context
requestLogger := logger.With("request_id", requestID, "method", r.Method)
requestLogger.Info("handling request")
```

## Recommended Tools

| Category | Tool | Why |
|----------|------|-----|
| HTTP router | `net/http` (1.22+) or `chi` | stdlib has method routing now; chi adds middleware chain |
| Database | `pgx` v5 | High-performance PostgreSQL driver, pool management |
| Migrations | `goose` or `golang-migrate` | SQL-based migrations, embed support |
| Validation | `go-playground/validator` | Struct tag validation |
| Config | `envconfig` or `koanf` | Typed environment variable loading |
| Logging | `log/slog` (stdlib) | Structured, leveled, built-in |
| Testing | `testify` + `gomock` | Assertions and interface mocking |
| Linting | `golangci-lint` | Meta-linter, runs 50+ linters |
| HTTP client | `net/http` + `resty` | stdlib is fine; resty adds retries |
| UUID | `google/uuid` | RFC 4122 UUIDs |

## Testing Patterns

### Table-Driven Tests

```go
func TestProjectService_Create(t *testing.T) {
    tests := []struct {
        name    string
        input   CreateProjectInput
        setup   func(repo *MockProjectRepository)
        wantErr error
    }{
        {
            name:  "success",
            input: CreateProjectInput{Name: "My Project"},
            setup: func(repo *MockProjectRepository) {
                repo.EXPECT().GetByName(gomock.Any(), "My Project").Return(nil, ErrNotFound)
                repo.EXPECT().Create(gomock.Any(), gomock.Any()).Return(nil)
            },
            wantErr: nil,
        },
        {
            name:  "duplicate name",
            input: CreateProjectInput{Name: "Existing"},
            setup: func(repo *MockProjectRepository) {
                repo.EXPECT().GetByName(gomock.Any(), "Existing").Return(&model.Project{}, nil)
            },
            wantErr: ErrAlreadyExists,
        },
        {
            name:  "empty name",
            input: CreateProjectInput{Name: ""},
            setup: func(repo *MockProjectRepository) {},
            wantErr: ErrValidation,
        },
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            ctrl := gomock.NewController(t)
            repo := NewMockProjectRepository(ctrl)
            tt.setup(repo)

            svc := NewProjectService(repo, slog.Default())
            _, err := svc.Create(context.Background(), tt.input)

            if tt.wantErr != nil {
                assert.ErrorIs(t, err, tt.wantErr)
            } else {
                assert.NoError(t, err)
            }
        })
    }
}
```

### Integration Tests with testcontainers

```go
func TestPostgresProjectRepository(t *testing.T) {
    if testing.Short() {
        t.Skip("skipping integration test")
    }

    ctx := context.Background()
    container, err := postgres.Run(ctx, "postgres:16-alpine",
        postgres.WithDatabase("testdb"),
    )
    require.NoError(t, err)
    t.Cleanup(func() { container.Terminate(ctx) })

    connStr, _ := container.ConnectionString(ctx)
    db := mustConnect(connStr)

    repo := NewPostgresProjectRepository(db)

    t.Run("create and get", func(t *testing.T) {
        project := &model.Project{Name: "Test"}
        err := repo.Create(ctx, project)
        require.NoError(t, err)

        got, err := repo.Get(ctx, project.ID)
        require.NoError(t, err)
        assert.Equal(t, "Test", got.Name)
    })
}
```

### HTTP Handler Tests

```go
func TestProjectHandler_Create(t *testing.T) {
    ctrl := gomock.NewController(t)
    svc := NewMockProjectService(ctrl)
    handler := NewProjectHandler(svc)

    body := `{"name": "Test Project"}`
    req := httptest.NewRequest(http.MethodPost, "/projects", strings.NewReader(body))
    req.Header.Set("Content-Type", "application/json")
    rec := httptest.NewRecorder()

    svc.EXPECT().Create(gomock.Any(), gomock.Any()).Return(&model.Project{
        ID:   uuid.New(),
        Name: "Test Project",
    }, nil)

    handler.Create(rec, req)

    assert.Equal(t, http.StatusCreated, rec.Code)
}
```

## golangci-lint Configuration

```yaml
# .golangci.yml
linters:
  enable:
    - errcheck
    - govet
    - staticcheck
    - unused
    - gosimple
    - ineffassign
    - typecheck
    - gocritic
    - gofumpt
    - misspell
    - prealloc
    - revive
    - bodyclose
    - contextcheck
    - nilerr
    - exhaustive

linters-settings:
  gocritic:
    enabled-tags:
      - diagnostic
      - style
      - performance
  revive:
    rules:
      - name: unexported-return
        disabled: true

issues:
  exclude-dirs:
    - vendor
```

## Common Mistakes

| Mistake | Impact | Fix |
|---------|--------|-----|
| Goroutine leaks (no shutdown path) | Memory grows indefinitely, connections pile up | Every goroutine must select on `ctx.Done()` or a done channel |
| Missing context cancellation | Child operations continue after parent cancelled | Always pass context through; use `errgroup.WithContext` for parallel work |
| Interface pollution (10+ method interfaces) | Hard to mock, violates ISP | Split into focused 1-3 method interfaces at the consumer site |
| `panic` in library/service code | Crashes the entire process | Only `panic` for truly unrecoverable programmer errors; return errors for everything else |
| Ignoring `Close()` errors on writers | Data loss (buffered data not flushed) | `defer` close but check: `if err := w.Close(); err != nil { ... }` |
| Not using `context.WithTimeout` for external calls | Hung connections block goroutines indefinitely | Wrap every DB query, HTTP call, gRPC call in a context with timeout |
| Mutable shared state without sync | Data races, unpredictable behavior | Use `sync.Mutex`, channels, or `sync.Map`; run tests with `-race` |
| `init()` functions with side effects | Hard to test, hidden dependencies | Explicit initialization in `main.go` |
| Returning `interface{}` / `any` from functions | Loses type safety, requires type assertions everywhere | Return concrete types; use generics (Go 1.18+) when appropriate |
| Not running `go vet` and `golangci-lint` in CI | Bugs caught only at runtime | Add `golangci-lint run` to CI pipeline and pre-commit hooks |
