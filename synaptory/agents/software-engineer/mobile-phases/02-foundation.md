# Phase 2: Project Setup & Foundation

## Objective

Establish the project structure, navigation architecture, theme system, and shared components so screens can be built in parallel. This phase creates the skeleton that every screen plugs into.

## 2.1 Project Initialization

### React Native (Expo) — Default

```bash
npx create-expo-app mobile --template expo-template-blank-typescript
cd mobile
npx expo install expo-router expo-linking expo-constants expo-secure-store
npx expo install react-native-reanimated react-native-gesture-handler react-native-screens react-native-safe-area-context
npm install @tanstack/react-query zustand axios zod
npm install -D jest @testing-library/react-native
```

### Flutter (if selected)

```bash
flutter create --org com.example --platforms ios,android mobile
cd mobile
# Add dependencies to pubspec.yaml: go_router, riverpod, dio, freezed, json_annotation
flutter pub get
```

Project structure (React Native / Expo):
```
mobile/
├── app.json                    # Expo config
├── tsconfig.json               # TypeScript strict mode
├── package.json
├── metro.config.js             # Metro bundler config
├── src/
│   ├── app/                    # Expo Router entry (if using file-based routing)
│   ├── screens/                # Screen components by feature group
│   ├── components/             # Reusable UI components
│   ├── navigation/             # Navigator definitions, route types
│   ├── services/               # API client, storage, push
│   ├── hooks/                  # Custom hooks
│   ├── stores/                 # Zustand stores
│   ├── theme/                  # Design tokens, theme provider
│   ├── utils/                  # Helpers, formatters, validators
│   └── types/                  # Shared TypeScript types
├── assets/                     # Images, fonts, animations
├── tests/                      # Unit and E2E tests
├── ios/                        # iOS native project (managed by Expo)
├── android/                    # Android native project (managed by Expo)
└── eas.json                    # EAS Build config
```

## 2.2 Navigation Architecture

Create `mobile/src/navigation/`:

```
navigation/
├── RootNavigator.tsx           # Entry point — auth check, deep link config
├── AuthNavigator.tsx           # Login, signup, forgot-password stacks
├── MainTabNavigator.tsx        # Bottom tabs (home, explore, create, profile)
├── types.ts                    # Route param types (typed navigation)
├── linking.ts                  # Deep link configuration
└── guards.ts                   # Auth guard, onboarding guard
```

Requirements:
- **Typed navigation** — All route params defined in `types.ts`. No `any` params. `useNavigation<ScreenProps>()` everywhere.
- **Deep linking** — Every screen reachable via URL scheme (`myapp://`) and universal links (`https://myapp.com/`)
- **Auth guard** — Unauthenticated users see AuthNavigator. Authenticated users see MainTabNavigator. Transition is seamless (no flash).
- **Conditional stacks** — Onboarding stack shown only on first launch. Admin screens only for admin role.
- **Tab bar** — Bottom tabs with icons, badges for notifications, haptic feedback on tap

Navigation pattern:
```
RootNavigator
├── AuthNavigator (Stack)
│   ├── Login
│   ├── Signup
│   ├── ForgotPassword
│   └── VerifyEmail
├── OnboardingNavigator (Stack) — first launch only
│   ├── Welcome
│   ├── Permissions
│   └── ProfileSetup
└── MainTabNavigator (Tabs)
    ├── HomeStack
    │   ├── Home
    │   └── Detail
    ├── ExploreStack
    │   ├── Explore
    │   └── SearchResults
    ├── CreateStack
    │   └── Create (modal presentation)
    ├── NotificationsStack
    │   └── Notifications
    └── ProfileStack
        ├── Profile
        ├── EditProfile
        └── Settings
```

## 2.3 Theme System & Design Tokens

Create `mobile/src/theme/`:

```
theme/
├── tokens.ts                   # Color, spacing, typography, radii, shadows
├── colors.ts                   # Light and dark color palettes
├── typography.ts               # Font families, sizes, line heights, weights
├── spacing.ts                  # 4px base unit scale
├── ThemeProvider.tsx            # React context for theme switching
└── useTheme.ts                 # Hook to consume theme values
```

Token standards:
- **Colors** — Neutral scale (50-950). One primary color. Semantic: success, warning, danger. Both light and dark palettes. Respect system appearance setting.
- **Typography** — Platform-native font stacks (SF Pro on iOS, Roboto on Android). Modular scale. Dynamic Type / font scaling support.
- **Spacing** — 4px base: 0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48.
- **Radii** — sm (4px), md (8px), lg (12px), xl (16px), full (9999px).
- **Shadows** — Platform-appropriate: `shadowOffset`/`shadowRadius` on iOS, `elevation` on Android.
- **Motion** — Respect `prefers-reduced-motion`. Default durations: fast (150ms), normal (300ms), slow (500ms).

## 2.4 Shared Components

Create `mobile/src/components/`:

Build the foundational shared components that every screen will import:

```
components/
├── Button.tsx                  # Primary, secondary, outline, ghost, destructive variants
├── Input.tsx                   # Text input with label, error, helper text, icons
├── Card.tsx                    # Container with header, body, footer
├── Avatar.tsx                  # User avatar with fallback initials
├── Badge.tsx                   # Status badges with color variants
├── LoadingSpinner.tsx          # Platform-native activity indicator
├── Skeleton.tsx                # Loading skeleton for content placeholders
├── EmptyState.tsx              # Empty state with icon, title, description, CTA
├── ErrorState.tsx              # Error state with retry button
├── Toast.tsx                   # Toast notification system
├── ListItem.tsx                # Standard list row with chevron, icon, subtitle
├── Divider.tsx                 # Section divider
├── ScreenWrapper.tsx           # SafeAreaView + KeyboardAvoidingView + StatusBar
└── index.ts                    # Barrel export
```

Every component MUST:
- Use design tokens from theme (no hardcoded colors, sizes, or spacing)
- Support both light and dark mode
- Handle dynamic type / font scaling
- Include accessible labels and hints
- Support minimum 44x44pt touch targets

## 2.5 API Client Setup

Create `mobile/src/services/api.ts`:

- Typed Axios/Dio instance with base URL from environment config
- Auth token injection via interceptor (token from secure storage)
- Token refresh with request queue (avoid parallel refresh races)
- Error standardization (network error vs server error vs auth error)
- Request/response logging in development mode

## Validation Loop

Before moving to Phase 3:
- Project builds and runs on both iOS simulator and Android emulator
- Navigation flows between all navigator groups (auth → main, tab switching)
- Theme provider works with system appearance (light/dark)
- Shared components render correctly on both platforms
- API client makes authenticated requests
- Deep link config resolves at least one test URL

**Do NOT present foundation for approval — these are building blocks. Move to screens.**

## Quality Bar

- TypeScript strict mode enabled, zero `any` types
- Navigation is fully typed — no untyped route params
- Every component uses theme tokens — no hardcoded visual values
- Both platforms build without warnings
- Safe area insets handled in ScreenWrapper
- Keyboard avoidance configured for both platforms
