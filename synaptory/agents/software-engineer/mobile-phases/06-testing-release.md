# Phase 6: Testing & Release

## Objective

Establish comprehensive test coverage, verify accessibility compliance on both platforms, prepare app store metadata, and configure release builds. This runs AFTER Phase 5 (Polish), so E2E tests capture the final production experience including animations and native integrations.

## Context Bridge

Read Phase 1 screen inventory from `.synaptory/software-engineer/mobile/docs/screen-inventory.md`. Read Phase 4 native capabilities from `.synaptory/software-engineer/mobile/docs/native-capabilities.md`. Reference the navigation map from Phase 2 for route coverage.

## Workflow

### Step 1: Unit Testing

Framework: Jest + @testing-library/react-native (React Native) or `flutter_test` (Flutter)

Test every layer:

**Hooks:**
- Custom hooks tested with `renderHook` — auth, connectivity, keyboard, permissions
- Mock native modules (`jest.mock('expo-secure-store')`, etc.)
- Test hook lifecycle: mount, state changes, cleanup

**Services:**
- API client tested with MSW (Mock Service Worker) or manual mocks
- Test token refresh flow, error standardization, retry logic
- Test offline queue: enqueue, persist, replay on reconnect

**Stores:**
- Zustand/Redux stores tested in isolation
- Test state transitions, selectors, async actions
- Test persistence layer (hydrate from storage, write back)

**Utils:**
- Formatters, validators, helpers tested with pure function tests
- Edge cases: empty strings, null values, boundary numbers, unicode text

**Components:**
- Every shared component tested across all variants and states
- Test accessibility: `getByRole`, `getByLabelText`, accessible props
- Test user interaction: press, long press, text input, toggle
- Test error boundaries: simulate render error, verify fallback UI

Coverage target: 80% branch coverage on business logic (hooks, services, stores, utils). 60% on screen components (focus on interaction and state, not layout).

Produce tests in `mobile/tests/unit/`.

### Step 2: End-to-End Testing

Framework: Detox (React Native, recommended) or Maestro (cross-framework) or `integration_test` (Flutter)

#### Detox Configuration

```javascript
// .detoxrc.js
module.exports = {
  testRunner: { args: { config: 'e2e/jest.config.js' } },
  apps: {
    'ios.release': { type: 'ios.app', binaryPath: '...', build: '...' },
    'android.release': { type: 'android.apk', binaryPath: '...', build: '...' }
  },
  devices: {
    simulator: { type: 'ios.simulator', device: { type: 'iPhone 15' } },
    emulator: { type: 'android.emulator', device: { avdName: 'Pixel_7' } }
  },
  configurations: {
    'ios.sim.release': { device: 'simulator', app: 'ios.release' },
    'android.emu.release': { device: 'emulator', app: 'android.release' }
  }
};
```

#### Critical E2E Flows

Write E2E tests for every critical user flow from Phase 1:

| Flow | Test Steps | Both Platforms |
|------|-----------|----------------|
| **Signup** | Open app → navigate to signup → fill form → submit → verify email screen → verify redirect to home | Yes |
| **Login** | Open app → fill login → submit → verify home screen loads | Yes |
| **Biometric login** | (mock biometric) Open app → biometric prompt → verify home screen | Yes |
| **Core CRUD** | Login → create item → verify in list → edit item → verify changes → delete → verify removed | Yes |
| **Navigation** | Login → visit every tab → verify content renders → deep link to detail → verify screen | Yes |
| **Push notification** | (mock notification) Receive notification → tap → verify correct screen opens | Yes |
| **Offline mode** | Login → disable network → verify cached data shown → create item → enable network → verify sync | Yes |
| **Settings** | Login → settings → change theme → verify theme applied → change notifications → verify saved | Yes |

#### Maestro Alternative

If Detox setup is too heavy, use Maestro for quick cross-platform E2E:

```yaml
# maestro/flows/login.yaml
appId: com.example.myapp
---
- launchApp
- tapOn: "Email"
- inputText: "test@example.com"
- tapOn: "Password"
- inputText: "password123"
- tapOn: "Sign In"
- assertVisible: "Home"
```

Produce E2E tests in `mobile/tests/e2e/`.

### Step 3: Accessibility Audit

**Automated:**
- Every component test verifies accessible labels exist (`getByLabelText`, `getByRole`)
- Use `jest-native` matchers: `toHaveAccessibilityValue`, `toHaveAccessibilityState`
- Run `accessibility-inspector` audit on iOS Simulator
- Run `accessibility-scanner` on Android Emulator

**Manual checklist:**
- VoiceOver (iOS): Every screen navigable. Every element announced correctly. Actions discoverable.
- TalkBack (Android): Every screen navigable. Focus order logical. Custom actions announced.
- Dynamic Type: App usable at maximum font size. No text truncation that hides meaning. Layouts adapt.
- Color contrast: 4.5:1 minimum for normal text, 3:1 for large text and UI elements.
- Touch targets: Minimum 44x44pt on iOS, 48x48dp on Android.
- Motion: `prefers-reduced-motion` respected. Essential animations still convey meaning.

Produce `.synaptory/software-engineer/mobile/docs/a11y-audit.md`.

### Step 4: Performance Testing

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Cold start** | < 3s to interactive | Stopwatch from tap to first interaction |
| **Warm start** | < 1s to content | Stopwatch from tap to content visible |
| **Scroll FPS** | ≥ 55fps sustained | React Native Perf Monitor / Android GPU Profiling |
| **JS bundle** | < 2MB uncompressed | Metro bundle analysis |
| **App download** | < 50MB (iOS), < 30MB (Android) | Archive build size |
| **Memory** | < 200MB peak | Xcode Instruments / Android Profiler |
| **API response** | < 2s perceived | Loading skeleton shown, content renders on data arrival |

Produce `.synaptory/software-engineer/mobile/docs/performance-report.md`.

### Step 5: App Store Metadata

#### iOS — App Store Connect

Prepare in `.synaptory/software-engineer/mobile/docs/app-store-ios.md`:
- App name (30 char max)
- Subtitle (30 char max)
- Description (4000 char max)
- Keywords (100 char max, comma-separated)
- Category and subcategory
- Privacy policy URL
- Support URL
- Screenshots: 6.7" (iPhone 15 Pro Max), 6.5" (iPhone 14 Plus), 5.5" (iPhone 8 Plus), 12.9" (iPad Pro)
- App privacy details (data collection declarations)

#### Android — Google Play Console

Prepare in `.synaptory/software-engineer/mobile/docs/app-store-android.md`:
- App name (50 char max)
- Short description (80 char max)
- Full description (4000 char max)
- Category
- Content rating questionnaire answers
- Privacy policy URL
- Screenshots: phone, 7" tablet, 10" tablet
- Feature graphic (1024x500)
- Data safety section declarations

### Step 6: Release Build Configuration

#### EAS Build (Expo)

Create `mobile/eas.json`:
```json
{
  "cli": { "version": ">= 5.0.0" },
  "build": {
    "development": { "developmentClient": true, "distribution": "internal" },
    "preview": { "distribution": "internal" },
    "production": { "autoIncrement": true }
  },
  "submit": {
    "production": {
      "ios": { "appleId": "PLACEHOLDER", "ascAppId": "PLACEHOLDER" },
      "android": { "serviceAccountKeyPath": "PLACEHOLDER" }
    }
  }
}
```

#### Code Signing
- iOS: Provisioning profiles managed by EAS or manual Xcode setup
- Android: Upload keystore generated and stored securely (not in repo)
- Document signing setup in `.synaptory/software-engineer/mobile/docs/release-config.md`

## Output Files

- `mobile/tests/unit/` — Unit tests for hooks, services, stores, components
- `mobile/tests/e2e/` — Detox/Maestro E2E tests
- `mobile/.detoxrc.js` or `mobile/maestro/` — E2E framework config
- `mobile/eas.json` — EAS Build configuration
- `.synaptory/software-engineer/mobile/docs/a11y-audit.md`
- `.synaptory/software-engineer/mobile/docs/performance-report.md`
- `.synaptory/software-engineer/mobile/docs/app-store-ios.md`
- `.synaptory/software-engineer/mobile/docs/app-store-android.md`
- `.synaptory/software-engineer/mobile/docs/release-config.md`

## Validation Loop

Before concluding the mobile skill:
- [ ] Unit tests cover hooks, services, stores, and shared components (80% branch coverage on logic)
- [ ] E2E tests cover all critical user flows on both platforms
- [ ] Accessibility audit complete — VoiceOver and TalkBack verified
- [ ] Performance targets met (cold start, scroll FPS, bundle size, memory)
- [ ] App store metadata prepared for both iOS and Android
- [ ] Release build configuration ready (EAS or native build)
- [ ] Both platforms build without errors or warnings

**Present testing summary with coverage report, a11y audit results, performance scores, and release readiness to user.**

## Quality Bar

Every screen must be tested on both platforms. "Tests pass on iOS" is not acceptable — "78 unit tests (82% branch coverage), 8 E2E flows on iOS + Android, zero VoiceOver/TalkBack issues, cold start 2.1s, scroll 58fps, bundle 1.6MB, app size 42MB iOS / 28MB Android" is acceptable.
