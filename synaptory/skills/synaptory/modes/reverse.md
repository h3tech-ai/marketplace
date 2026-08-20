
# Crew Reverse

Reads an existing codebase and reconstructs its architecture, business rules, dependencies, risks, and test coverage. All output is knowledge artifacts — this skill never modifies production code.

**Re-runnable**: Run multiple times. Each run detects what changed since the last run and updates context packages incrementally.

## Protocols

!`cat .synaptory/.protocols/ux-protocol.md 2>/dev/null || true`
!`cat .synaptory/.protocols/tool-efficiency.md 2>/dev/null || true`
!`cat .synaptory/.protocols/visual-identity.md 2>/dev/null || true`
!`cat .synaptory/.protocols/receipt-protocol.md 2>/dev/null || true`
!`cat .synaptory.yaml 2>/dev/null || echo "No config — run /synaptory and say 'initialize' first"`

## Step 1: Initialization

### 1a. Read or Generate Config

Read `.synaptory.yaml` for project config.

**If `.synaptory.yaml` exists**: Extract `project.language`, `project.framework`, `project.architecture`, `paths.*`, `brownfield.*`, `connections.*`. Skip to Step 1b.

**If `.synaptory.yaml` does not exist**: Auto-detect and generate it inline:

1. **Detect project structure** (parallel scans):
   ```python
   # Language detection — majority wins
   Glob("**/*.ts"), Glob("**/*.py"), Glob("**/*.go"), Glob("**/*.rs"),
   Glob("**/*.java"), Glob("**/*.cs"), Glob("**/*.rb"), Glob("**/*.php")

   # Framework detection
   Glob("next.config.*"), Glob("nest-cli.json"), Glob("angular.json"),
   Glob("manage.py"), Glob("go.mod"), Glob("Cargo.toml"), Glob("pom.xml"),
   Glob("Gemfile"), Glob("composer.json")

   # Architecture detection
   Glob("docker-compose*"), Glob("**/Dockerfile"),
   Glob("services/*/"), Glob("apps/*/"), Glob("packages/*/")

   # CI/CD detection
   Glob(".github/workflows/*"), Glob(".gitlab-ci.yml"),
   Glob("Jenkinsfile"), Glob(".circleci/*")

   # Frontend detection
   Glob("**/package.json"), Glob("**/tsconfig.json"),
   Glob("src/pages/*"), Glob("src/app/*")
   ```

2. **Health assessment** (parallel):
   ```python
   # Test density
   test_count = len(Glob("**/*{test,spec,_test}*.*"))
   source_count = len(Glob("**/*.{ts,js,py,go,rs,java,cs,rb,php}"))
   test_density = test_count / source_count if source_count > 0 else 0

   # Tech debt markers
   Grep("TODO|HACK|FIXME|XXX|WORKAROUND", type="[detected language]")

   # Git metrics
   Bash("git rev-list --count HEAD 2>/dev/null || echo 0")

   # Coverage config
   Glob(".nycrc*"), Glob("jest.config*"), Glob(".coveragerc"), Glob("jacoco*")

   # Documentation level
   Glob("docs/**/*.md"), Glob("README.md")
   ```

3. **Classify project type**:
   - `greenfield`: no source files OR <10 git commits
   - `brownfield`: >100 source files AND test density <0.1 AND >20 TODO/HACK markers
   - `evolved_brownfield`: >50 source files AND test density >0.3 AND documentation present
   - `mixed`: doesn't fit above categories

4. **Confirm with user**:
   ```python
   AskUserQuestion(questions=[{
     "question": f"Detected: {language} / {framework} / {architecture}\n"
       f"Project type: {project_type}\n"
       f"Health: test density {test_density:.1%}, {debt_count} debt markers, {commit_count} commits\n\n"
       f"I'll generate .synaptory.yaml with these settings. Correct?",
     "header": "Auto-init",
     "options": [
       {"label": "Yes, generate config", "description": "Write .synaptory.yaml and continue with analysis"},
       {"label": "Let me adjust", "description": "I'll correct the detection before generating"}
     ],
     "multiSelect": false
   }])
   ```

5. **Write `.synaptory.yaml`** from template `${CLAUDE_SKILL_DIR}/../_shared/templates/synaptory.yaml.tmpl`, filling in detected values. Continue with analysis.

### 1b. Detect Existing Context Packages

```python
Glob(".synaptory/.orchestrator/context-packages/*.md")
```

### 1c. Quick Codebase Metrics

Run in parallel:
```python
# Source files
Glob("**/*.{ts,js,tsx,jsx,py,go,rs,java,php,rb,cs,scala,kt}")

# Test files
Glob("**/*{test,spec,_test}*.{ts,js,py,go,rs,java,php,rb}")
Glob("**/test*/**/*.{ts,js,py,go,rs,java,php,rb}")
Glob("**/__tests__/**/*")

# Tech debt markers
Grep("TODO|HACK|FIXME|XXX|WORKAROUND", type="[detected language]")

# Git depth
Bash("git rev-list --count HEAD 2>/dev/null || echo 0")

# Git age
Bash("git log --reverse --format='%ci' | head -1 2>/dev/null || echo unknown")
```

### 1d. Present Execution Plan

```
┌─ Discover mode ──────────────────────────────────────────────┐
│                                                              │
│  Codebase: [language], [framework]                           │
│  Size: [N] source files, [N] test files                      │
│  Git: [N] commits, active since [date]                       │
│  Tech debt markers: [N] TODO, [N] HACK, [N] FIXME           │
│  Existing context packages: [Yes — N packages / No]          │
│                                                              │
│  What would you like to analyze?                             │
│                                                              │
│  > Full analysis (Recommended)                               │
│    Architecture & dependencies only                          │
│    Business rules & domain knowledge only                    │
│    Coverage baseline & characterization tests only           │
│    Update existing packages with new information             │
│    Chat about this                                           │
└──────────────────────────────────────────────────────────────┘
```

```python
AskUserQuestion(questions=[{
  "question": f"Detected: {language}, {source_count} source files, {test_count} test files, "
    f"{commit_count} commits. "
    f"{'Context packages exist from prior run.' if existing_packages else 'No prior analysis found.'}\n\n"
    f"What would you like to analyze?",
  "header": "Discover mode",
  "options": [
    {"label": "Full analysis (Recommended)", "description": "Architecture + business rules + risks + coverage + PRD synthesis. Complete understanding."},
    {"label": "Architecture & dependencies only", "description": "Module mapping, dependency graph, hidden coupling. Skip business rules and tests."},
    {"label": "Business rules & domain knowledge only", "description": "Extract business logic, assumptions, tech debt. Skip architecture mapping."},
    {"label": "Coverage baseline & tests only", "description": "Measure coverage, generate characterization tests. Skip architecture and rules."},
    {"label": "Update existing packages", "description": "Scan for changes since last run. Merge new findings into existing packages."},
    {"label": "Chat about this", "description": "Discuss what Discover mode does and how it helps"}
  ],
  "multiSelect": false
}])
```

### 1e. Live Sources (Optional)

After analysis option selection, prompt the user for optional live source access. Check `.synaptory.yaml` for pre-configured `connections` first — if URL or connection string are already set, confirm them instead of asking from scratch.

```python
AskUserQuestion(questions=[
  {
    "question": "Do you have a running instance of this application I can explore?\n"
      "This lets me discover screens, forms, navigation flows, and API contracts at runtime.",
    "header": "Web app",
    "options": [
      {"label": "Yes — provide URL", "description": "I'll crawl the running app to extract UI structure and API contracts"},
      {"label": "Skip", "description": "Code-only analysis (no live app exploration)"}
    ],
    "multiSelect": false
  },
  {
    "question": "Do you have a database connection I can query?\n"
      "This lets me verify the actual schema, constraints, and cross-reference against code.",
    "header": "Database",
    "options": [
      {"label": "Yes — provide connection", "description": "I'll extract live schema, indexes, constraints, and flag code/DB drift"},
      {"label": "Skip", "description": "Code-only analysis (no live database query)"}
    ],
    "multiSelect": false
  }
])
```

If user provides a webapp URL:
- Ask for credentials if needed (auth_type: form, basic, bearer)
- Record URL and auth details for Step 2.5

If user provides a database connection:
- Auto-detect engine from codebase (scan for ORM configs, driver imports, connection strings)
- Confirm detected engine with user
- Record connection string for Step 3.5

### 1f. Bootstrap Workspace

```bash
mkdir -p .synaptory/.orchestrator/context-packages/
mkdir -p .synaptory/reverse-engineering/architecture/impact-templates/
mkdir -p .synaptory/reverse-engineering/domain/
mkdir -p .synaptory/reverse-engineering/coverage/characterization-tests/
mkdir -p .synaptory/reverse-engineering/live-exploration/
mkdir -p .synaptory/reverse-engineering/database/
```

## Step 2: Architecture Reconstruction

```
━━━ Discover mode ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [1/5] Architecture Reconstruction
    ⧖ scanning module boundaries...
```

### 2a. Module Inventory

Scan the codebase to identify top-level modules/services. For each:
- Name and directory path
- Primary language and framework
- Entry points (main files, controllers, handlers, route definitions)
- Approximate LOC

Write to `.synaptory/reverse-engineering/architecture/module-inventory.md`

### 2b. Dependency Graph

For each module, trace dependencies:

```python
# Import/require analysis
Grep("import |require\\(|from |use |include", path="[module dir]")

# API call detection
Grep("fetch\\(|axios\\.|http\\.|HttpClient|RestTemplate|requests\\.", path="[module dir]")

# Database query detection
Grep("SELECT |INSERT |UPDATE |DELETE |CREATE TABLE|query\\(|execute\\(", path="[module dir]")

# Message queue detection
Grep("publish\\(|subscribe\\(|emit\\(|on\\(.*event|kafka|rabbit|sqs", path="[module dir]")
```

For each dependency found, record:
- **From**: source module
- **To**: target module/service/database
- **Type**: api_call | shared_db | shared_lib | message_queue | file_system
- **Fragility**: HIGH (no contract/schema) | MEDIUM (implicit contract) | LOW (explicit contract/types)

### 2c. Hidden Coupling Detection

Specifically scan for non-obvious coupling:

```python
# Shared database tables — multiple modules querying same table
Grep("FROM [table_name]|INTO [table_name]|UPDATE [table_name]")
# Cross-reference: if two modules reference the same table → hidden coupling

# Shared types/enums — same type imported in multiple modules
Grep("enum |type |interface |class ", path="[shared directories]")

# Temporal coupling — ordering assumptions
Grep("sleep|wait|setTimeout|after|depends_on|order", path="[module dir]")

# Polling patterns
Grep("setInterval|schedule|cron|polling|poll", path="[module dir]")
```

For each hidden coupling:
- Modules involved
- Coupling mechanism
- Code pattern that revealed it
- Risk if one side changes

Write to `.synaptory/reverse-engineering/architecture/hidden-coupling.md`

### 2d. Interface Contracts (As-Built)

For each inter-module interface:
- Protocol (REST, gRPC, queue, shared DB)
- Endpoints/channels with request/response formats
- Authentication method
- Error handling behavior
- Whether the contract is documented or implicit

### 2e. Architecture Pattern Classification

Based on module inventory and dependency graph, classify:
- **Monolith**: single deployable, shared codebase
- **Modular monolith**: single deployable, clear module boundaries
- **Microservices**: multiple deployables, API boundaries
- **Distributed monolith**: multiple deployables but tightly coupled (anti-pattern — flag this)

### 2f. Impact Analysis Templates

For each module, generate:

```markdown
# Impact Analysis: [module-name]

## If you change the public interface:
- [list of modules that call this]
- [downstream effects]

## If you change the database schema:
- [other modules sharing these tables]
- [stored procs/views depending on this schema]

## Safe change zones (internal-only, no cross-module impact):
- [internal files with no external callers]
```

Write to `.synaptory/reverse-engineering/architecture/impact-templates/[module]-impact.md`

### 2g. Write Context Packages

Write summaries to `.synaptory/.orchestrator/context-packages/`:
- `dependency-map.md` — from template `${CLAUDE_SKILL_DIR}/../_shared/templates/context-packages/dependency-map.tmpl.md`
- `interface-contracts.md` — from template `${CLAUDE_SKILL_DIR}/../_shared/templates/context-packages/interface-contracts.tmpl.md`

Context packages are **summaries** for session injection. Full detailed analysis stays in `reverse-engineering/architecture/`.

```
  [1/N] Architecture Reconstruction
    ✓ {N} modules mapped
    ✓ {N} dependencies, {M} hidden couplings
    ✓ {N} interface contracts documented
    ✓ {N} impact templates generated
    ✓ Pattern: [monolith/microservices/modular monolith]
```

## Step 2.5: Live App Exploration (Optional)

**Skip this step if**: no webapp URL was provided in Step 1e.

```
  [+] Live App Exploration
    ⧖ crawling running application...
```

### Tool Selection

Based on the URL provided:
- **Local** (`localhost`, `127.0.0.1`, or `0.0.0.0`): Use **Preview MCP** tools. Create a temporary entry in `.claude/launch.json` if needed, or use `preview_eval` to navigate to the URL. Use `preview_snapshot`, `preview_click`, `preview_fill`, `preview_network`, `preview_inspect`.
- **Remote** (any other URL): Use **Claude in Chrome** tools. Use `navigate`, `read_page`, `find`, `form_input`, `computer` (for screenshots), `read_network_requests`.

### Phase A: Authentication

If credentials were provided in Step 1e:

1. Navigate to the URL
2. Detect login form:
   - Preview: `preview_snapshot` → look for form elements with password inputs
   - Chrome: `find("login form")` or `find("sign in")`
3. Fill credentials:
   - Preview: `preview_fill(selector, value)` for username and password fields
   - Chrome: `form_input(ref, value)` for each field
4. Submit and verify successful login:
   - Preview: `preview_click("button[type=submit]")` → `preview_snapshot` → verify no login form present
   - Chrome: `computer(action="left_click", ref=submit_ref)` → `read_page` → verify
5. Record observed auth method (form-based, redirect to SSO, basic auth dialog, etc.)

If no credentials: attempt to access the URL directly. If redirected to login, note this in the output and proceed with whatever screens are accessible.

### Phase B: Screen Discovery (Breadth-First Crawl)

1. Capture the landing page:
   - Preview: `preview_snapshot` → record accessibility tree as screen entry
   - Chrome: `read_page` → record accessibility tree as screen entry
2. Extract all navigation elements (menus, sidebars, tabs, breadcrumbs, footer links):
   - Preview: `preview_snapshot` → filter for `role=link`, `role=menuitem`, `role=tab`
   - Chrome: `find("navigation links")` or `read_page(filter="interactive")`
3. For each unvisited navigation link (breadth-first):
   a. Navigate to it:
      - Preview: `preview_click(selector)` or `preview_eval("window.location = '...'")`
      - Chrome: `computer(action="left_click", ref=link_ref)` or `navigate(url)`
   b. Capture snapshot → record as screen entry
   c. Extract new navigation links not yet in the visited set
   d. Navigate back or to the next unvisited link
4. **Cap at 50 screens** to avoid runaway crawling. If cap reached, note how many links remain unexplored.
5. For each screen, record:
   - URL/route path
   - Page title (from DOM)
   - Accessibility tree summary (headings, forms, tables, buttons, data displays)
   - Navigation links found on this screen
   - Whether it requires authentication

### Phase C: Form Schema Extraction

For each screen containing form elements (detected in Phase B snapshots):

1. Identify all form inputs:
   - Preview: `preview_inspect("form")` → enumerate inputs, selects, textareas
   - Chrome: `find("form inputs")` → read attributes
2. For each field, record:
   - Label text (associated `<label>` or `aria-label`)
   - Name/ID attribute
   - Input type (text, email, password, select, checkbox, radio, date, number, file)
   - Required attribute
   - Validation attributes (pattern, min, max, minlength, maxlength, step)
   - Dropdown options (for select elements)
3. Record form action (submit URL/method if observable from DOM attributes)
4. Check for client-side validation:
   - Preview: `preview_eval("document.querySelectorAll('[data-validation], .error, .invalid')")`
   - Chrome: `javascript_tool` to inspect validation-related attributes

### Phase D: API Contract Discovery

1. Clear network request log:
   - Preview: start fresh monitoring
   - Chrome: `read_network_requests(clear=true)`
2. Navigate through 3-5 key workflows (identified from primary navigation flows):
   - Fill and submit forms with sample data
   - Click action buttons (save, delete, search, filter)
   - Load data-heavy pages (tables, dashboards)
3. After each workflow, capture network requests:
   - Preview: `preview_network` → list all requests
   - Chrome: `read_network_requests(urlPattern="/api/")` → list API calls
4. For each XHR/Fetch request:
   - Record: method, URL path, request headers (content-type, auth), request body structure, response status, response body structure
   - Preview: `preview_network(requestId=...)` to inspect individual response bodies
   - Chrome: `read_network_requests` captures request details
5. Deduplicate endpoints (same method+path → merge observed request/response variations)
6. Group by base URL path for documentation

### Phase E: Write Outputs

Write detailed findings to `.synaptory/reverse-engineering/live-exploration/`:
- `screen-inventory.md` — all discovered screens with accessibility tree summaries
- `form-schemas.md` — all form field definitions with validation rules
- `api-contracts-live.md` — API calls observed during exploration
- `navigation-map.md` — screen connectivity graph and primary flows

Write context package summary to `.synaptory/.orchestrator/context-packages/ui-contracts.md` — from template `${CLAUDE_SKILL_DIR}/../_shared/templates/context-packages/ui-contracts.tmpl.md`

```
  [+] Live App Exploration
    ✓ {N} screens discovered
    ✓ {N} forms mapped ({M} fields total)
    ✓ {N} API endpoints observed
    ✓ {N} navigation flows identified
    ✓ Auth method: [form-based / basic / bearer / SSO / none / not provided]
```

## Step 3: Business Rule & Domain Extraction

```
  [2/5] Business Rule & Domain Extraction
    ⧖ scanning for business logic patterns...
```

### 3a. Business Rule Extraction

For each module, read business logic files (services, domain models, validators, calculators).

Grep patterns to find business rules:
```python
# Pricing/financial logic
Grep("price|cost|discount|tax|fee|rate|total|amount|calculate", type="[lang]")

# Validation rules
Grep("validate|check|verify|assert|ensure|must|require", type="[lang]")

# Status/state transitions
Grep("status|state|transition|workflow|approve|reject|cancel", type="[lang]")

# Access control
Grep("permission|role|authorize|allow|deny|can_|has_access", type="[lang]")

# Date/time business logic
Grep("expire|deadline|due_date|schedule|period|quarter|fiscal", type="[lang]")
```

For each identifiable rule:
```
Rule ID: BR-[module]-[NNN]
Location: [file path]:[line range]
What the code does: [plain language]
Inferred business reason: [why this likely exists]
Confidence: HIGH (unit tested) | MEDIUM (partial evidence) | LOW (untested) | INFERRED (no tests, derived from reading)
Evidence: [specific code pattern]
Assumption risk: [what could be wrong]
```

**Rules**:
1. Never infer business rules from variable names alone — require logic evidence
2. Always cite file:line for every rule
3. Write "uncertain about Y because Z" when confidence is LOW
4. If existing documentation contradicts code, flag the discrepancy

### 3b. Implicit Assumption Detection

Scan for unvalidated assumptions:
```python
# Magic numbers
Grep("[^0-9][0-9]{2,}[^0-9]|0\\.[0-9]+", type="[lang]")  # Constants not named

# Hardcoded strings
Grep("\"(http|https|ftp|localhost|127\\.0|192\\.168)", type="[lang]")

# Date/timezone assumptions
Grep("UTC|timezone|tz|locale|getTime|Date\\.now|datetime\\.now", type="[lang]")

# NULL handling
Grep("null|nil|None|undefined|Optional|nullable", type="[lang]")
```

Write to `.synaptory/reverse-engineering/domain/implicit-assumptions.md`

### 3c. Dead Code Candidates

```python
# Find all public function/method definitions
Grep("(export |public |def |func |fn )(function |class |async )?\\w+", type="[lang]")

# For each, search for callers
Grep("[function_name]\\(", type="[lang]")
# If zero callers → candidate (but flag reflection/dynamic dispatch risk)
```

**Never declare code dead with full confidence.** Always write: "Candidate for dead code — requires runtime verification. Risk: [reflection / dynamic dispatch / external caller / CLI entry point]"

Write to `.synaptory/reverse-engineering/domain/dead-code-candidates.md`

### 3d. Technical Debt Taxonomy

Extract and categorize all TODO/HACK/FIXME comments:
```python
Grep("TODO|HACK|FIXME|XXX|WORKAROUND|TEMP|KLUDGE", type="[lang]", output_mode="content")
```

Categorize each:
- **Intentional shortcut**: developer knew this was temporary (TODO with explanation)
- **Outdated pattern**: code uses patterns obsolete in current language/framework version
- **Unknown origin**: hard to understand with no clear reason
- **Workaround**: compensates for a bug or limitation in a dependency

Write to `.synaptory/reverse-engineering/domain/technical-debt-register.md`

### 3e. Write Context Packages

Write to `.synaptory/.orchestrator/context-packages/`:
- `business-rules-inventory.md` — from template
- `data-dictionary.md` (if database entities detected) — key entities, table meanings, relationships

```
  [2/N] Business Rule & Domain Extraction
    ✓ {N} business rules extracted ({M} high confidence, {K} need verification)
    ✓ {N} implicit assumptions documented
    ✓ {N} dead code candidates identified
    ✓ {N} tech debt items categorized
```

## Step 3.5: Live Database Schema Analysis (Optional)

**Skip this step if**: no database connection was provided in Step 1e.

```
  [+] Live Database Schema Analysis
    ⧖ querying database schema...
```

### 3.5a. Auto-Detect Database Engine

If `connections.database.engine` is `auto` or not set, detect from the codebase:

```python
# ORM config files
Glob("prisma/schema.prisma")       # → PostgreSQL/MySQL/SQLite/SQL Server
Glob("**/ormconfig.*")             # → TypeORM config
Glob("**/knexfile.*")              # → Knex.js config
Glob("**/alembic.ini")             # → SQLAlchemy/Alembic (Python)
Glob("**/database.yml")            # → Rails ActiveRecord
Glob("**/sequelize*config*")       # → Sequelize config

# Driver imports in code
Grep("require\\(['\"]pg['\"]\\)|from ['\"]pg['\"]|import.*psycopg2|import.*asyncpg", type="[lang]")  # → PostgreSQL
Grep("require\\(['\"]mysql2?['\"]\\)|from ['\"]mysql|import.*pymysql|import.*MySQLdb", type="[lang]")  # → MySQL
Grep("require\\(['\"]better-sqlite3['\"]\\)|import.*sqlite3", type="[lang]")  # → SQLite
Grep("require\\(['\"]mssql['\"]\\)|import.*pyodbc|import.*pymssql", type="[lang]")  # → SQL Server
Grep("require\\(['\"]mongoose['\"]\\)|from ['\"]pymongo|import.*motor", type="[lang]")  # → MongoDB

# Connection strings in env/config
Grep("postgres://|postgresql://", glob="*.env*")  # → PostgreSQL
Grep("mysql://", glob="*.env*")                    # → MySQL
Grep("mongodb://|mongodb\\+srv://", glob="*.env*") # → MongoDB
```

Confirm detected engine with user if ambiguous (multiple engines detected).

### 3.5b. Schema Extraction

Connect to the database using Bash with the appropriate CLI tool. **All queries are read-only** — never execute DDL or DML.

**PostgreSQL** (psql):
```bash
# List all tables
psql "$CONNECTION_STRING" -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;"

# For each table — columns with types, defaults, constraints
psql "$CONNECTION_STRING" -c "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name = '[table]' ORDER BY ordinal_position;"

# Foreign keys
psql "$CONNECTION_STRING" -c "SELECT tc.constraint_name, kcu.column_name, ccu.table_name AS foreign_table, ccu.column_name AS foreign_column FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name WHERE tc.constraint_type = 'FOREIGN KEY';"

# Indexes
psql "$CONNECTION_STRING" -c "SELECT indexname, tablename, indexdef FROM pg_indexes WHERE schemaname = 'public';"

# Views
psql "$CONNECTION_STRING" -c "SELECT table_name, view_definition FROM information_schema.views WHERE table_schema = 'public';"

# Functions/procedures
psql "$CONNECTION_STRING" -c "SELECT routine_name, routine_type, data_type FROM information_schema.routines WHERE routine_schema = 'public';"

# Check constraints
psql "$CONNECTION_STRING" -c "SELECT tc.constraint_name, tc.table_name, cc.check_clause FROM information_schema.table_constraints tc JOIN information_schema.check_constraints cc ON tc.constraint_name = cc.constraint_name WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = 'public';"

# Row count estimates
psql "$CONNECTION_STRING" -c "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"
```

**MySQL** (mysql):
```bash
mysql -e "SHOW TABLES;" "$DB_NAME" -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS"
mysql -e "DESCRIBE [table];" "$DB_NAME" -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS"
mysql -e "SELECT * FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = '$DB_NAME';" -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS"
mysql -e "SELECT * FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = '$DB_NAME';" -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS"
mysql -e "SHOW CREATE TABLE [table];" "$DB_NAME" -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS"
mysql -e "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = '$DB_NAME';" -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS"
```

**SQLite** (sqlite3):
```bash
sqlite3 "$DB_PATH" ".tables"
sqlite3 "$DB_PATH" ".schema [table]"
sqlite3 "$DB_PATH" "PRAGMA table_info([table]);"
sqlite3 "$DB_PATH" "PRAGMA foreign_key_list([table]);"
sqlite3 "$DB_PATH" "PRAGMA index_list([table]);"
```

**SQL Server** (sqlcmd):
```bash
sqlcmd -S "$DB_HOST" -U "$DB_USER" -P "$DB_PASS" -d "$DB_NAME" -Q "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE';"
sqlcmd -S "$DB_HOST" -U "$DB_USER" -P "$DB_PASS" -d "$DB_NAME" -Q "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '[table]';"
```

**MongoDB** (mongosh):
```bash
mongosh "$CONNECTION_STRING" --eval "db.getCollectionNames()"
mongosh "$CONNECTION_STRING" --eval "db.[collection].findOne()"  # Sample document for schema inference
mongosh "$CONNECTION_STRING" --eval "db.[collection].getIndexes()"
mongosh "$CONNECTION_STRING" --eval "db.[collection].estimatedDocumentCount()"
```

### 3.5c. Cross-Reference with Code

After extracting the live schema, compare against code-level data models:

1. **Find ORM models/entities in code**:
   ```python
   # Prisma
   Read("prisma/schema.prisma")
   # TypeORM / Sequelize / Drizzle
   Grep("@Entity|@Table|@Column|Model\\.init|defineModel|createTable", type="[lang]")
   # SQLAlchemy / Django
   Grep("class.*Base\\)|class.*Model\\)|models\\.Model", type="py")
   # ActiveRecord
   Grep("class.*<.*ActiveRecord|has_many|belongs_to|has_one", type="rb")
   ```

2. **Flag schema drift** — mismatches between code and database:
   - Columns in DB but not referenced in any ORM model → `extra_column`
   - Columns referenced in code but missing from DB → `column_missing`
   - Type mismatches (e.g., code treats as string, DB has integer) → `type_mismatch`
   - Constraints in DB with no code-level validation → `constraint_gap`
   - ORM model references table that doesn't exist → `table_missing`

3. **Map stored procedures to code callers**:
   ```python
   # For each procedure name found in DB
   Grep("[procedure_name]", type="[lang]")
   # Flag procedures with zero callers as ORPHANED
   ```

4. **Identify database-level business rules**:
   - CHECK constraints → business validation rules
   - Triggers → side-effect rules (audit logging, cascading updates)
   - Computed/generated columns → derived value rules
   - Default values → implicit business defaults
   - UNIQUE constraints beyond PKs → uniqueness rules

### 3.5d. Write Outputs

Write detailed findings to `.synaptory/reverse-engineering/database/`:
- `schema-live.md` — full schema documentation (tables, views, procedures, indexes)
- `schema-drift.md` — code vs database mismatches with risk assessment
- `db-business-rules.md` — constraints, triggers, computed columns as business rules

Write context package summary to `.synaptory/.orchestrator/context-packages/data-schema.md` — from template `${CLAUDE_SKILL_DIR}/../_shared/templates/context-packages/data-schema.tmpl.md`

```
  [+] Live Database Schema Analysis
    ✓ Engine: [PostgreSQL/MySQL/SQLite/SQL Server/MongoDB]
    ✓ {N} tables, {M} views, {K} stored procedures
    ✓ {N} foreign key relationships
    ✓ {N} schema drift items (code vs database)
    ✓ {N} database-level business rules extracted
```

## Step 4: Risk & Hotspot Analysis

```
  [3/N] Risk & Hotspot Analysis
    ⧖ analyzing git history for hotspots...
```

### 4a. Git-Based Hotspot Analysis

```bash
# File change frequency (last 12 months)
git log --since="12 months ago" --name-only --pretty=format: | sort | uniq -c | sort -rn | head -30

# Per-file author count (bus factor)
git log --since="12 months ago" --format='%an' -- [file] | sort -u | wc -l

# Files with most commits by single author (bus factor risk)
# For top 20 most-changed files, check author distribution
```

For each hotspot file:
- Commit count (12 months)
- Author count
- Bus factor risk: CRITICAL (>80% commits from one author AND P1 module) | HIGH | MEDIUM | LOW
- Assessment: stable churn (healthy iteration) vs chaotic churn (recurring bugs)

### 4b. Risk Scoring Per Module

Score each module: `coupling_score × complexity_score × coverage_gap`

- **Coupling score**: number of inbound + outbound dependencies + hidden couplings
- **Complexity score**: LOC + cyclomatic complexity indicators (nested conditionals, long functions)
- **Coverage gap**: 1 - (test files / source files for this module)

### 4c. Incident Pattern Analysis (Optional)

If user provides an incident log file path:
1. Read and parse (flexible format — adapt to CSV, JSON, markdown, plain text)
2. Group incidents by affected module
3. Identify recurring root cause patterns
4. Map each pattern to specific code areas
5. Identify time patterns (end-of-month, high-load periods, batch job days)

If no incident log provided: note the gap in the receipt. Generate interview questions about incident history in the tribal knowledge templates.

### 4d. Tribal Knowledge Interview Templates

For each P1 (high-risk) module, generate structured interview questions:

```markdown
# Tribal Knowledge Capture: [module]

## For senior developers who have worked on this module

### About the business rules
1. What does this module do that is NOT obvious from the code?
2. Are there calculations or rules that look wrong but are intentionally that way?
3. What edge cases do you know about that have no tests?

### About the dependencies
4. Are there undocumented external systems this module talks to?
5. What happens when [dependency X] is slow or unavailable?

### About the history
6. What was the most significant bug in this module in the last 2 years?
7. What workarounds exist in this code and why?

### About risks
8. What would you be most afraid to change?
9. What implicit assumptions does this module make about its input data?
10. Are there seasonal or load-based behaviors that only appear under specific conditions?
```

Write to `.synaptory/reverse-engineering/domain/tribal-knowledge-templates.md`

### 4e. Write Context Package

Write to `.synaptory/.orchestrator/context-packages/risk-register.md` — from template.

```
  [3/N] Risk & Hotspot Analysis
    ✓ {N} hotspot files identified
    ✓ {N} bus factor risks flagged
    ✓ {N} risk items registered
    ✓ {N} tribal knowledge templates generated
    {✓ Incident log analyzed: {N} patterns found | ○ No incident log provided}
```

## Step 5: Coverage Baseline

```
  [4/N] Coverage Baseline
    ⧖ measuring test coverage...
```

### 5a. Coverage Measurement

Detect coverage tool:
```python
Glob(".nycrc*"), Glob("jest.config*"), Glob(".coveragerc"), Glob("coverage/"), Glob("jacoco*")
Grep("coverage|istanbul|nyc|pytest-cov|jacoco", path="package.json")
Grep("coverage|pytest-cov", path="pyproject.toml")
```

If coverage tool found: attempt to read last coverage report.
If no coverage tool: estimate from test file presence per module.

### 5b. Risk-Based Prioritization

Rank all source files by: `business_risk × (1 - current_coverage)`

Business risk indicators:
- File contains extracted business rules → HIGH
- File handles money, auth, or PII → HIGH
- File in hidden coupling report → HIGH
- File has high in-degree (many callers) → MEDIUM
- File is a git hotspot → MEDIUM

Write to `.synaptory/reverse-engineering/coverage/risk-priority-map.md`:
```
| File | Risk Score | Coverage | Priority | Business Rules | Hidden Coupling |
|---|---|---|---|---|---|
| [file] | [1-10] | [%] | P1/P2/P3 | [Y/N] | [Y/N] |
```

### 5c. Characterization Test Generation

If `brownfield.characterization_tests: true` in config (default):

For P1 files (highest risk, lowest coverage):
1. Read the file's public interface (exported functions, class methods)
2. For each public function:
   a. Identify input types and boundary values
   b. Write a test that calls the function and asserts the CURRENT return value
   c. Mark every test: `// CHARACTERIZATION TEST — captures existing behavior as of [date]. If this fails, verify the change was intentional before updating.`
3. Write tests to `.synaptory/reverse-engineering/coverage/characterization-tests/[module]/`

**Test naming**: `[original-filename].characterization.[test-ext]`
Example: `pricing.service.characterization.test.ts`

**Rules for characterization tests**:
1. Test what the code DOES, not what it SHOULD do
2. Every assertion captures observed behavior, not business intent
3. Include edge cases from implicit assumptions report
4. Never assert business correctness — only behavioral consistency
5. If you cannot determine the actual output (e.g., needs database), write a test skeleton with `// TODO: Run this test locally to capture the actual output`

### 5d. Coverage Baseline File

Write `.synaptory/reverse-engineering/coverage/coverage-baseline.json`:
```json
{
  "generated_at": "[ISO timestamp]",
  "tool": "[coverage tool or 'estimated']",
  "files": {
    "[relative file path]": {
      "coverage_percent": 0,
      "risk_tier": "P1",
      "has_characterization_tests": true,
      "last_measured": "[ISO timestamp]"
    }
  }
}
```

### 5e. Write Context Package

Write to `.synaptory/.orchestrator/context-packages/health-assessment.md` — from template. Include overall score, dimension scores, module-level health scores.

```
  [4/N] Coverage Baseline
    ✓ {N} files analyzed
    ✓ P1: {N} files, P2: {N} files, P3: {N} files
    ✓ Coverage baseline: {N}%
    ✓ {N} characterization tests generated
    ✓ Coverage ratchet baseline written
```

## Step 6: PRD Synthesis

**Skip this step if**: user selected a partial analysis mode (architecture-only, rules-only, coverage-only). Only runs on **full analysis** or **update**.

```
  [5/N] PRD Synthesis
    ⧖ synthesizing findings into Product Requirements Document...
```

> The PRD documents the **existing system as-is** for reimplementation or modernization planning. It is NOT a feature spec — for new feature requirements, see the BRD produced by the PM agent during `/synaptory`.

### 6a. Read All Analysis Artifacts

Read in parallel:
```python
# Context packages
Read(".synaptory/.orchestrator/context-packages/dependency-map.md")
Read(".synaptory/.orchestrator/context-packages/interface-contracts.md")
Read(".synaptory/.orchestrator/context-packages/business-rules-inventory.md")
Read(".synaptory/.orchestrator/context-packages/risk-register.md")
Read(".synaptory/.orchestrator/context-packages/health-assessment.md")
Read(".synaptory/.orchestrator/context-packages/ui-contracts.md")       # if exists
Read(".synaptory/.orchestrator/context-packages/data-schema.md")        # if exists

# Detailed analysis
Read(".synaptory/reverse-engineering/architecture/module-inventory.md")
Read(".synaptory/reverse-engineering/architecture/hidden-coupling.md")
Read(".synaptory/reverse-engineering/domain/implicit-assumptions.md")
Read(".synaptory/reverse-engineering/domain/dead-code-candidates.md")
Read(".synaptory/reverse-engineering/domain/technical-debt-register.md")
Read(".synaptory/reverse-engineering/coverage/risk-priority-map.md")

# Live exploration outputs (if Steps 2.5/3.5 ran)
Read(".synaptory/reverse-engineering/live-exploration/screen-inventory.md")   # if exists
Read(".synaptory/reverse-engineering/live-exploration/form-schemas.md")       # if exists
Read(".synaptory/reverse-engineering/live-exploration/api-contracts-live.md") # if exists
Read(".synaptory/reverse-engineering/database/schema-live.md")               # if exists
Read(".synaptory/reverse-engineering/database/schema-drift.md")              # if exists
Read(".synaptory/reverse-engineering/database/db-business-rules.md")         # if exists
```

### 6b. Cross-Reference & Conflict Detection

Before writing the PRD, systematically identify conflicts across analysis dimensions:

1. **Entity conflicts**: Compare ORM model properties (from code) vs DB columns (from data-schema) vs API response shapes (from ui-contracts). Flag fields with different names, types, or constraints for the same concept.

2. **Business rule conflicts**: Compare rules extracted from code (business-rules-inventory) vs rules from DB constraints (data-schema DB rules) vs validation rules from UI forms (ui-contracts form schemas). Flag rules enforced at one layer but missing from others.

3. **Coverage gaps**: Cross-reference modules containing business rules (business-rules-inventory) with test coverage (health-assessment). Flag modules with critical rules but no tests.

4. **Dead references**: If Step 3.5 ran — check code referencing tables/columns that don't exist in DB. If Step 2.5 ran — check routes in code with no corresponding screens, or screens with no code-level handlers.

5. **Contract mismatches**: Compare code-level interface contracts (interface-contracts) with runtime-observed API calls (ui-contracts). Flag endpoints with different request/response shapes.

Collect all conflicts as numbered Open Questions for the PRD.

### 6c. Write PRD

Write to `.synaptory/reverse-engineering/PRD.md` using template `${CLAUDE_SKILL_DIR}/../_shared/templates/prd.tmpl.md`.

**PRD Sections** (include only sections with sufficient source material — omit sections with no data rather than writing empty sections):

1. **Overview** — purpose, users, scale, classification
2. **Architecture Summary** — pattern, module map, dependency overview
3. **Domain Model** — entities, relationships, data flow
4. **User Interfaces & Screens** (only if Step 2.5 ran) — screen inventory, form schemas, navigation
5. **Business Rules & Logic** — all rules grouped by module with confidence and criticality
6. **Workflows & Processes** — user and system workflows
7. **API & Interface Contracts** — inter-module + runtime-observed APIs
8. **Data Schema** (only if Step 3.5 ran) — tables, stored procedures, schema drift
9. **Integrations & External Dependencies** — external services and APIs
10. **Security & Access Control** — auth methods, roles, permissions
11. **Risk Assessment** — critical risks, hotspots, hidden coupling
12. **Technical Debt & Known Issues** — debt taxonomy, dead code, implicit assumptions
13. **Test Coverage & Quality** — coverage baseline, priority gaps
14. **Open Questions** — all conflicts from Step 6b + unverifiable assumptions
15. **Migration Considerations** — schema drift, breaking changes, data volumes
16. **Glossary** — domain terms from code, DB, and UI
17. **Sources** — all analysis files with timestamps

### 6d. PRD Quality Rules

1. **Synthesis only** — document what analyses found, never speculate beyond reported facts
2. **Cite sources** — every claim references `[context-package:section]` or `[file:line]`
3. **Behaviour over implementation** — describe what the system does, not how the code works internally
4. **Include only what exists** — omit sections lacking source material rather than writing empty placeholders
5. **Flag uncertainty** — prefix unverified claims with `UNVERIFIED:` and add to Open Questions
6. **Exhaustive for implementation** — complete enough to serve as the single reference for reimplementation planning

```
  [5/N] PRD Synthesis
    ✓ {N} sections written
    ✓ {N} cross-reference conflicts → Open Questions
    ✓ {N} business rules consolidated
    ✓ PRD: .synaptory/reverse-engineering/PRD.md
```

## Step 7: Assembly & Report

```
  [6/N] Assembly & Final Report
    ⧖ consolidating context packages...
```

### 7a. Verify All Artifacts Written

Check that these files exist in `.synaptory/.orchestrator/context-packages/`:
- `dependency-map.md`
- `interface-contracts.md`
- `business-rules-inventory.md`
- `risk-register.md`
- `health-assessment.md`
- `ui-contracts.md` (only if live app exploration ran in Step 2.5)
- `data-schema.md` (only if live database analysis ran in Step 3.5)

Check that PRD exists (if full analysis):
- `.synaptory/reverse-engineering/PRD.md`

If any are missing (due to partial execution), note in the receipt.

### 7b. Write Receipt

Write `.synaptory/reverse-engineering/receipt.json`:
```json
{
  "skill": "Discover mode",
  "completed_at": "[ISO timestamp]",
  "run_type": "full | architecture_only | rules_only | coverage_only | update",
  "summary": {
    "modules_mapped": 0,
    "dependencies_found": 0,
    "hidden_couplings": 0,
    "business_rules_extracted": 0,
    "high_confidence_rules": 0,
    "low_confidence_rules": 0,
    "risk_items": 0,
    "dead_code_candidates": 0,
    "tech_debt_items": 0,
    "hotspot_files": 0,
    "bus_factor_risks": 0,
    "coverage_baseline_percent": 0,
    "characterization_tests_generated": 0,
    "incident_log_analyzed": false,
    "live_app_explored": false,
    "screens_discovered": 0,
    "forms_mapped": 0,
    "api_endpoints_observed": 0,
    "live_db_analyzed": false,
    "db_tables_verified": 0,
    "schema_drift_items": 0,
    "db_business_rules": 0,
    "prd_generated": false,
    "prd_sections": 0,
    "open_questions": 0,
    "cross_reference_conflicts": 0
  },
  "context_packages_written": [],
  "warnings": [],
  "gaps": []
}
```

### 7c. Print Completion Summary

```
━━━ Discover mode Complete ━━━━━━━━━━━━━━━━━━━━━━━━━━ ⏱ Xm Ys ━━
  Modules mapped:       {N}
  Dependencies:         {N} ({M} hidden couplings)
  Business rules:       {N} ({M} high confidence, {K} need verification)
  Risk items:           {N} (critical: {A}, high: {B})
  Dead code candidates: {N}
  Tech debt items:      {N}
  Hotspot files:        {N} ({M} bus factor risks)
  Coverage baseline:    {N}%
  Char. tests:          {N} generated
  {If live app explored:}
  Screens discovered:   {N} ({M} forms, {K} API endpoints)
  {If live DB analyzed:}
  DB tables verified:   {N} ({M} schema drift, {K} DB rules)

  PRD:                  .synaptory/reverse-engineering/PRD.md
                        {N} sections, {M} open questions

  Context packages: .synaptory/.orchestrator/context-packages/
  Full analysis:    .synaptory/reverse-engineering/

  Next steps:
  → Review PRD at .synaptory/reverse-engineering/PRD.md
  → /synaptory "modernize"         Plan modernization using PRD
  → /synaptory "add [feature]"     Build with full legacy context
  → /synaptory "harden"            Security + quality audit with context
  → /synaptory                 Re-run to update with new information
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Re-Run Behavior

When `.synaptory/.orchestrator/context-packages/` already has files and user selects "Update existing packages":

1. Read all existing context packages
2. Read the previous receipt: `.synaptory/reverse-engineering/receipt.json`
3. Detect what changed since last run:
   ```bash
   # Files modified since last run
   git diff --name-only --since="[receipt.completed_at]" 2>/dev/null
   # New files since last run
   git diff --diff-filter=A --name-only --since="[receipt.completed_at]" 2>/dev/null
   ```
4. For changed/new files only:
   - Re-analyze architecture (if module structure changed)
   - Re-extract business rules (if business logic files changed)
   - Re-assess risks (if hotspot files changed)
   - Re-measure coverage (if test files changed)
5. **Merge** new findings into existing packages:
   - Add new rules with new IDs (don't re-number existing)
   - Update existing entries if their source files changed
   - Flag entries whose source files were deleted as "STALE — source file removed"
   - Update all timestamps
6. Write updated receipt with `run_type: "update"`

## Adaptive Behaviors

| Situation | Action |
|---|---|
| No git history | Skip hotspot analysis, note gap in receipt |
| No test files found | Coverage = 0%, flag as HIGH RISK, generate characterization tests as priority |
| Multi-language codebase (3+) | Prioritize interface contracts between language boundaries |
| Oracle/SQL Server detected | Scan for stored procedures, map proc-to-proc call chains |
| Mono-repo detected (apps/) | Treat each app as a separate module for analysis |
| >500 source files | Focus Phase 3 business rules on files with highest coupling + lowest coverage |
| User provides incident log | Activate incident pattern analysis in Step 4c |
| Existing documentation found | Cross-reference against code, flag discrepancies |
| Webapp URL provided (local) | Run Step 2.5 using **Preview MCP** tools (preview_snapshot, preview_click, preview_network) |
| Webapp URL provided (remote) | Run Step 2.5 using **Claude in Chrome** tools (navigate, read_page, find, read_network_requests) |
| Database connection provided | Run Step 3.5: query live schema, cross-reference with code ORM models, flag drift |
| No webapp URL / no DB connection | Skip Steps 2.5 and 3.5, proceed with code-only analysis (current default behavior) |
| DB engine ambiguous (multiple detected) | Ask user to confirm which engine to use before querying |

## Rules

1. **Never modify production code.** All outputs are analysis artifacts
2. **Always cite file:line** for every finding — no unsourced claims
3. **Surface uncertainty.** Write "uncertain about X because Y" rather than guessing
4. **Dead code is never certain.** Always flag reflection/dynamic dispatch risk
5. **Characterization tests capture behavior, not intent.** Never assert business correctness
6. **Context packages are summaries.** Keep them concise for session injection. Full analysis lives in `reverse-engineering/`
7. **Additive re-runs.** Never delete existing context package entries — add, update, or flag as stale
