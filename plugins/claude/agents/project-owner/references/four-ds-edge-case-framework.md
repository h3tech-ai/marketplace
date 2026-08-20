# Four D's Edge Case Framework

Systematic method for discovering edge cases in every Feature. Apply all four categories plus supplementary checks to each feature before writing user stories.

---

## The Four D's

### 1. Disconnections

What happens when connectivity is lost during the user action?

**Check for:**
- Network loss mid-form-submission (data in flight)
- Network loss mid-file-upload (partial upload)
- WebSocket/SSE disconnection during real-time updates
- API timeout between microservices
- Database connection pool exhaustion
- Third-party service unreachable (payment gateway, auth provider, email service)
- CDN/asset loading failure

**For each disconnection, specify:**
- What the user sees (loading state? error message? stale data?)
- What happens to in-flight data (lost? queued for retry? saved locally?)
- Recovery behavior (auto-retry? manual retry button? offline queue?)
- Idempotency requirements (can the action be safely retried?)

### 2. Destruction

What happens when data is deleted, corrupted, or lost?

**Check for:**
- User deletes their own data (account, records, files)
- Admin deletes another user's data
- Cascade deletion (deleting a parent removes children — is that intended?)
- Soft delete vs hard delete (can data be recovered?)
- Data corruption from concurrent writes (race conditions)
- Bulk operations that partially fail (5 of 10 records deleted — what about the other 5?)
- Import/migration that overwrites existing data
- Cache invalidation leaving stale references

**For each destruction scenario, specify:**
- Confirmation required? (single click vs "type DELETE to confirm")
- Undo/recovery window? (30s undo? trash bin? audit log?)
- What cascades? (dependent records, foreign keys, cached references)
- Who is notified? (other users affected by the deletion)

### 3. Deception

What happens when a bad actor or malformed input enters the system?

**Check for:**
- SQL injection in all text fields
- XSS in all user-generated content displayed to others
- CSRF on state-changing operations
- Mass assignment / parameter tampering
- Replay attacks (resubmitting captured requests)
- Rate limiting bypass (distributed attacks)
- Privilege escalation (modifying another user's resources)
- File upload abuse (executable files, oversized files, path traversal)
- Enumeration attacks (sequential IDs, username/email existence checks)
- Input that breaks rendering (extremely long strings, special chars, RTL text, emoji, null bytes)

**For each deception scenario, specify:**
- Input validation rules (server-side — never trust client-side only)
- Exact error response (status code, error code, safe message — never leak internals)
- Rate limiting thresholds
- Audit logging requirements (log the attempt for security review)

### 4. Delays

What happens when the system is slow or unresponsive?

**Check for:**
- API response takes > 5 seconds (show loading indicator? timeout?)
- Background job takes > 30 seconds (progress bar? notification when done?)
- Third-party service responds slowly (degrade gracefully? fallback?)
- Database query takes > 1 second under load (pagination? caching?)
- File processing is slow (queue with progress? email when done?)
- Real-time update is delayed (stale data displayed — user makes decisions on old data)
- Concurrent users cause contention (optimistic locking? queue? retry?)

**For each delay scenario, specify:**
- Timeout threshold (when does "slow" become "failed"?)
- Loading UX (skeleton, spinner, progress bar, optimistic update?)
- Timeout UX (error message with retry option?)
- Background processing UX (notification, polling, WebSocket push?)

---

## Supplementary Checks (Apply to Every Feature)

### Empty States
- What does the user see when there is NO data yet?
- First-time user experience (onboarding vs blank page)
- Search returns zero results
- Filter combination returns zero results
- List after all items are deleted

### Maximum Limits
- Maximum number of records/items (pagination? archiving?)
- Maximum field lengths (what happens at the boundary?)
- Maximum file size (rejection message? compression?)
- Maximum concurrent users on the same resource
- Maximum API request rate per user/tenant

### Concurrent Access
- Two users editing the same record simultaneously
- Two users submitting the same unique resource (e.g., same username)
- User submits a form, then navigates away before response
- Browser back button after form submission (re-submission prevention)
- Multiple browser tabs with the same session

---

## How to Apply

For each Feature, work through this checklist:

```markdown
## Edge Cases — [Feature Name]

### Disconnections
- [ ] Network loss during [primary action]
  - User sees: [specific UX]
  - Data fate: [lost/queued/saved]
  - Recovery: [auto-retry/manual/offline queue]

### Destruction
- [ ] User deletes [resource]
  - Confirmation: [none/dialog/type-to-confirm]
  - Cascade: [what else is affected]
  - Recovery: [undo window/trash/permanent]

### Deception
- [ ] [Input field] receives malicious input
  - Validation: [server-side rule]
  - Response: [status code + error body]
  - Logging: [what gets logged]

### Delays
- [ ] [Action] takes longer than [threshold]
  - Loading UX: [skeleton/spinner/progress]
  - Timeout: [threshold] → [error UX]
  - Fallback: [degrade/queue/notify]

### Empty State
- [ ] No [resource] exists yet → [specific UX]

### Max Limits
- [ ] More than [N] [resources] → [pagination/archive/error]

### Concurrent Access
- [ ] Two users [same action] simultaneously → [locking strategy]
```

Generate this checklist for every Feature. Edge cases discovered here become Negative Scenarios in User Stories.
