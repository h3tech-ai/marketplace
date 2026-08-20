# Phase 1: Platform Selection & Analysis

## Objective

Read BRD user stories and solution architect artifacts from the project root. Confirm platform, navigation, and state management choices with the user. Produce a structured analysis in `.synaptory/software-engineer/mobile/docs/` (workspace artifacts).

## Phase 0: Platform Selection

Before beginning analysis, confirm the platform with the user via AskUserQuestion:

### Platform Options

| Platform | Best For | Cross-Platform | Ecosystem |
|----------|----------|---------------|-----------|
| **React Native (Expo) (recommended)** | JS/TS teams, rapid iteration, broad library access, OTA updates | Yes — iOS + Android from one codebase | Largest RN ecosystem, Expo EAS for builds |
| **Flutter** | Pixel-perfect consistency, custom rendering, Dart teams | Yes — iOS + Android + Web from one codebase | Growing rapidly, strong Google backing |
| **Native iOS (Swift/SwiftUI)** | iOS-only products, deep Apple integration, max performance | No — iOS only | Full Apple SDK access, SwiftUI declarative |
| **Native Android (Kotlin/Jetpack Compose)** | Android-only products, deep Google integration | No — Android only | Full Android SDK access, Compose declarative |

### State Management Options

| Stack | Best For | Why |
|-------|----------|-----|
| **React Query + Zustand (recommended for RN)** | REST/GraphQL APIs with modest client state | Server state separated from client state, minimal boilerplate |
| **Redux Toolkit + RTK Query** | Complex offline-first state, large teams | Mature, predictable, excellent devtools |
| **Riverpod (Flutter)** | Flutter applications | Type-safe, testable, official recommendation |
| **Provider + BLoC (Flutter)** | Flutter with event-driven state | Separation of business logic from UI |

### Navigation Options

| Library | Platform | Why |
|---------|----------|-----|
| **React Navigation 6+ (recommended for RN)** | React Native | Most mature, deep linking, TypeScript support |
| **Expo Router** | React Native (Expo) | File-based routing, universal links built-in |
| **Go Router (Flutter)** | Flutter | Declarative, deep linking, redirect guards |
| **Navigation Compose (Android)** | Kotlin/Jetpack Compose | Official Jetpack navigation |
| **NavigationStack (iOS)** | Swift/SwiftUI | Native SwiftUI navigation |

**Engagement mode determines platform selection behavior:**
- **Autonomous**: Auto-select recommended defaults (React Native Expo + React Navigation + React Query + Zustand). Report selections in output. Do NOT ask.
- **Controlled**: Present all options via AskUserQuestion. Let user review and confirm.

## 1.1 Screen Flow Mapping

Create `.synaptory/software-engineer/mobile/docs/screen-flows.md`:

- Map every BRD user story to a screen or component
- Identify all distinct user flows (onboarding, auth, core CRUD, settings, notifications)
- Document navigation hierarchy (tab navigator, stack navigators, modal screens)
- Identify shared screen patterns (list-detail, form-submit, dashboard)
- Map role-based access per screen (which roles see which screens/sections)
- Identify gestures required (swipe-to-delete, pull-to-refresh, pinch-to-zoom)

## 1.2 Screen Inventory

Create `.synaptory/software-engineer/mobile/docs/screen-inventory.md`:

```markdown
| Screen | Navigator | Stack | Auth Required | Key Components | API Endpoints | Native Features |
|--------|-----------|-------|---------------|----------------|---------------|-----------------|
| Login | AuthStack | auth | No | LoginForm, OAuthButtons, BiometricPrompt | POST /auth/login | Biometrics |
| Home | MainTabs | home | Yes | FeedList, StatsCards, QuickActions | GET /dashboard | Push notifications |
| Camera | MainTabs | create | Yes | CameraPreview, MediaPicker | POST /media/upload | Camera, photo library |
| Profile | MainTabs | profile | Yes | ProfileHeader, SettingsList | GET /users/me | - |
| ... | ... | ... | ... | ... | ... | ... |
```

## 1.3 Native Capability Inventory

Create `.synaptory/software-engineer/mobile/docs/native-capabilities.md`:

- Catalog every native feature required by BRD user stories
- Classify by priority (P0 — core feature, P1 — enhancement, P2 — nice-to-have)
- Identify platform-specific implementation differences (iOS vs Android)
- Document permission requirements per feature
- Note offline requirements and sync strategy

| Capability | Priority | iOS Implementation | Android Implementation | Permission |
|-----------|----------|-------------------|----------------------|------------|
| Camera | P0 | AVCaptureSession / expo-camera | CameraX / expo-camera | camera |
| Push Notifications | P0 | APNs | FCM | notifications |
| Biometrics | P1 | Face ID / Touch ID | Fingerprint / Face Unlock | biometrics |
| Location | P1 | Core Location | Fused Location | location |
| Deep Links | P0 | Universal Links | App Links | - |
| Offline Storage | P0 | SQLite / MMKV | SQLite / MMKV | - |

## 1.4 API Surface Mapping

Cross-reference BRD user stories with OpenAPI specs:
- Map each screen to the API endpoints it consumes
- Identify real-time requirements (WebSocket, SSE, polling)
- Note optimistic update opportunities (like/unlike, mark-as-read)
- Document file upload flows and their endpoints (image, video, document)
- Identify pagination patterns per list endpoint (cursor-based recommended for mobile)
- Plan offline-first strategy: which endpoints need local caching, which mutations queue for retry

## Input Dependencies

### From Project Root
- `api/openapi/*.yaml` — OpenAPI 3.1 specs for typed client generation
- `docs/architecture/tech-stack.md` — Platform, language, auth provider decisions
- `docs/architecture/system-diagrams/` — C4 container diagrams for understanding service boundaries
- `docs/architecture/adrs/` — ADRs for auth strategy, API patterns
- `docs/architecture/ERD.md` — Entity relationships for understanding data shapes

### From BRD
- User stories with acceptance criteria
- User flow diagrams (onboarding, auth, core workflows, settings)
- Information architecture and navigation structure
- Role-based access requirements
- Branding guidelines and app icon (if provided)

## Validation Loop

Before moving to Phase 2:
- Platform, navigation, and state management choices resolved
- Screen flows mapped to navigators and stacks
- Screen inventory complete with routes, auth requirements, and API endpoints
- Native capability inventory classified by priority
- API surface mapped per screen with offline strategy noted

**Present analysis summary to user for quick review (no formal approval gate — informational).**

## Quality Bar

- Every BRD user story is mapped to at least one screen or component
- Every screen has its API endpoints identified
- Native capabilities documented with platform-specific implementation notes
- Navigation hierarchy is complete (no orphan screens)
- Offline strategy documented for data-critical flows
