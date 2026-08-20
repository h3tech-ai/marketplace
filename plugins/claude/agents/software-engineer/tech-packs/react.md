# Tech Pack: React (SPA)

> **Auto-loaded** when `.synaptory.yaml` specifies `react` in `tech-stack` or when `package.json` contains `react` as a dependency without `next` present. Applies to non-Next.js React applications (Vite, CRA, custom bundler).

This tech pack covers React single-page applications built with Vite. For Next.js projects, the `nextjs` tech pack is loaded instead — these two packs are mutually exclusive.

## Project Setup with Vite

**Always use Vite** for new React SPAs. CRA is deprecated. Webpack-based setups are only acceptable when extending an existing Webpack project.

```bash
npm create vite@latest my-app -- --template react-ts
```

### Vite Configuration

```ts
// vite.config.ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8080', // Backend proxy for development
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
          router: ['react-router'],
        },
      },
    },
  },
});
```

## React Router v7

Use React Router v7 with the data router API (loader/action pattern) for robust routing with data fetching.

```tsx
// src/router.tsx
import { createBrowserRouter, RouterProvider } from 'react-router';
import { RootLayout } from '@/layouts/RootLayout';
import { DashboardPage, dashboardLoader } from '@/pages/Dashboard';
import { ProtectedRoute } from '@/components/auth/ProtectedRoute';

const router = createBrowserRouter([
  {
    path: '/',
    element: <RootLayout />,
    errorElement: <RootErrorBoundary />,
    children: [
      { index: true, element: <HomePage /> },
      {
        element: <ProtectedRoute />,
        children: [
          {
            path: 'dashboard',
            element: <DashboardPage />,
            loader: dashboardLoader,
          },
          {
            path: 'settings',
            element: <SettingsLayout />,
            children: [
              { path: 'profile', element: <ProfilePage /> },
              { path: 'billing', element: <BillingPage /> },
            ],
          },
        ],
      },
    ],
  },
]);

export function App() {
  return <RouterProvider router={router} />;
}
```

### Route Protection Pattern

```tsx
// src/components/auth/ProtectedRoute.tsx
import { Navigate, Outlet, useLocation } from 'react-router';
import { useAuth } from '@/hooks/useAuth';

export function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) return <PageSkeleton />;

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <Outlet />;
}
```

## State Management

### Decision Matrix

| State Type | Solution | When |
|-----------|----------|------|
| Server state (API data) | React Query | Always — for any data fetched from an API |
| Global client state | Zustand | Auth state, theme, sidebar open/closed, user preferences |
| Local component state | `useState` | Form inputs, toggles, modals within a single component |
| Complex local state | `useReducer` | Multi-step forms, state machines within a component |
| URL state | React Router search params | Filters, pagination, sort order — anything that should be shareable via URL |

### Zustand (Recommended over Redux)

```tsx
// src/stores/auth-store.ts
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface AuthState {
  user: User | null;
  token: string | null;
  login: (credentials: LoginCredentials) => Promise<void>;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      token: null,
      login: async (credentials) => {
        const { user, token } = await authApi.login(credentials);
        set({ user, token });
      },
      logout: () => set({ user: null, token: null }),
    }),
    { name: 'auth-storage' }
  )
);
```

**Why Zustand over Redux:** Less boilerplate, no providers needed, no action creators or reducers, works outside React components, built-in persistence middleware. Redux is justified only for very large teams that need strict action logging and time-travel debugging.

### React Query for Server State

```tsx
// src/hooks/api/use-projects.ts
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { projectsApi } from '@/services/api';

export function useProjects(filters: ProjectFilters) {
  return useQuery({
    queryKey: ['projects', filters],
    queryFn: () => projectsApi.list(filters),
    staleTime: 5 * 60 * 1000, // 5 minutes
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: projectsApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}
```

**Never store API data in Zustand or useState.** React Query handles caching, deduplication, background refetching, loading/error states, and optimistic updates. Duplicating that in a client store creates stale data bugs.

## Form Handling

**Use React Hook Form + Zod** for all forms. This combination provides type-safe validation with minimal re-renders.

```tsx
// src/schemas/user.ts
import { z } from 'zod';

export const updateProfileSchema = z.object({
  name: z.string().min(1, 'Name is required').max(100),
  email: z.string().email('Invalid email'),
  bio: z.string().max(500).optional(),
});

export type UpdateProfileInput = z.infer<typeof updateProfileSchema>;

// src/components/forms/ProfileForm.tsx
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { updateProfileSchema, type UpdateProfileInput } from '@/schemas/user';

export function ProfileForm({ defaultValues }: { defaultValues: UpdateProfileInput }) {
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<UpdateProfileInput>({
    resolver: zodResolver(updateProfileSchema),
    defaultValues,
  });

  const onSubmit = async (data: UpdateProfileInput) => {
    await updateProfile.mutateAsync(data);
  };

  return (
    <form onSubmit={handleSubmit(onSubmit)}>
      <Input {...register('name')} error={errors.name?.message} />
      <Input {...register('email')} error={errors.email?.message} />
      <Textarea {...register('bio')} error={errors.bio?.message} />
      <Button type="submit" loading={isSubmitting}>Save</Button>
    </form>
  );
}
```

**Share Zod schemas between frontend and backend** when possible (monorepo or shared package). This guarantees validation consistency.

## Component Composition Patterns

### Compound Components

Use compound components for complex UI that has multiple related parts:

```tsx
// Usage
<DataTable data={users} columns={columns}>
  <DataTable.Toolbar>
    <DataTable.Search placeholder="Filter users..." />
    <DataTable.ColumnToggle />
  </DataTable.Toolbar>
  <DataTable.Content />
  <DataTable.Pagination />
</DataTable>
```

### Render Props (when composition is not enough)

```tsx
<Combobox<User>
  items={users}
  onSelect={setSelectedUser}
  renderItem={(user) => (
    <div className="flex items-center gap-2">
      <Avatar src={user.avatar} size="sm" />
      <span>{user.name}</span>
    </div>
  )}
/>
```

### Polymorphic Components

```tsx
interface ButtonProps<T extends React.ElementType = 'button'> {
  as?: T;
  children: React.ReactNode;
}

function Button<T extends React.ElementType = 'button'>({
  as,
  ...props
}: ButtonProps<T> & Omit<React.ComponentPropsWithoutRef<T>, keyof ButtonProps<T>>) {
  const Component = as || 'button';
  return <Component {...props} />;
}

// Usage
<Button>Click me</Button>              // Renders <button>
<Button as="a" href="/about">About</Button>  // Renders <a>
<Button as={Link} to="/dashboard">Go</Button> // Renders React Router Link
```

## Custom Hooks Patterns

### Rules for Custom Hooks

1. Extract repeated stateful logic into hooks — not just for reuse, but for testability
2. Name hooks `use{Domain}{Action}` — `useProjectList`, `useDebounce`, `useMediaQuery`
3. Return objects (not arrays) when there are more than 2 return values
4. Keep hooks focused — one responsibility per hook

```tsx
// src/hooks/use-debounce.ts
import { useState, useEffect } from 'react';

export function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedValue(value), delay);
    return () => clearTimeout(timer); // Cleanup on unmount or value change
  }, [value, delay]);

  return debouncedValue;
}

// src/hooks/use-media-query.ts
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => window.matchMedia(query).matches
  );

  useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, [query]);

  return matches;
}
```

## Code Splitting

Use `React.lazy` with `Suspense` for route-level code splitting:

```tsx
import { lazy, Suspense } from 'react';

const DashboardPage = lazy(() => import('@/pages/Dashboard'));
const SettingsPage = lazy(() => import('@/pages/Settings'));

// In router
{
  path: 'dashboard',
  element: (
    <Suspense fallback={<PageSkeleton />}>
      <DashboardPage />
    </Suspense>
  ),
}
```

**Split at route boundaries**, not at component level. Over-splitting creates waterfalls. Only split heavy feature components (rich text editor, chart library) that are not on the critical path.

## Error Boundaries

```tsx
// src/components/ErrorBoundary.tsx
import { Component, type ReactNode } from 'react';

interface Props {
  fallback: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      const { fallback } = this.props;
      return typeof fallback === 'function'
        ? fallback(this.state.error, this.reset)
        : fallback;
    }
    return this.props.children;
  }
}

// Usage
<ErrorBoundary
  fallback={(error, reset) => (
    <div>
      <p>Something went wrong: {error.message}</p>
      <Button onClick={reset}>Try again</Button>
    </div>
  )}
>
  <DataTable />
</ErrorBoundary>
```

Wrap each route segment and each independent widget in its own error boundary. One failing chart should not crash the entire dashboard.

## Recommended Libraries

| Category | Library | Why |
|----------|---------|-----|
| Bundler | `vite` | Fast dev server, ESM-native, excellent DX |
| Router | `react-router` v7 | Data loaders, nested routes, type-safe |
| Server state | `@tanstack/react-query` | Caching, deduplication, background refetch |
| Client state | `zustand` | Minimal, no boilerplate, no providers |
| Forms | `react-hook-form` + `@hookform/resolvers` | Minimal re-renders, Zod integration |
| Validation | `zod` | TypeScript-first, sharable with backend |
| UI | `shadcn/ui` or `radix-ui` | Accessible primitives, unstyled or copy-paste |
| HTTP | `ky` or `axios` | Interceptors, retry, timeout handling |
| Date | `date-fns` | Tree-shakable, immutable, small bundle |
| Tables | `@tanstack/react-table` | Headless, sorting, filtering, pagination |
| Testing | `vitest` + `@testing-library/react` | Vite-native, fast, user-centric tests |

## File Structure Convention

```
src/
  components/
    ui/                   # Primitives: Button, Input, Card, Modal
    layout/               # Header, Sidebar, Footer, PageLayout
    features/             # Domain: InvoiceTable, UserCard, ProjectList
    auth/                 # ProtectedRoute, LoginForm
  pages/                  # Route page components
    Dashboard.tsx
    Settings/
      Profile.tsx
      Billing.tsx
  hooks/                  # Custom hooks
    api/                  # React Query hooks per domain
      use-projects.ts
      use-users.ts
    use-debounce.ts
    use-media-query.ts
  services/               # API client layer
    api.ts                # Axios/ky instance with interceptors
    projects.ts           # Project API functions
  stores/                 # Zustand stores
    auth-store.ts
    ui-store.ts
  schemas/                # Zod schemas shared with forms
  types/                  # TypeScript types and interfaces
  lib/                    # Utilities, constants, helpers
  styles/                 # Global styles, CSS variables
  App.tsx                 # Root component with providers
  main.tsx                # Entry point
  router.tsx              # Route definitions
```

## Testing Patterns

```tsx
// Component test — test behavior, not implementation
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProfileForm } from './ProfileForm';

test('shows validation error for empty name', async () => {
  const user = userEvent.setup();
  render(<ProfileForm defaultValues={{ name: '', email: 'a@b.com' }} />);

  await user.clear(screen.getByLabelText('Name'));
  await user.click(screen.getByRole('button', { name: /save/i }));

  expect(screen.getByText('Name is required')).toBeInTheDocument();
});

// Hook test
import { renderHook, act } from '@testing-library/react';
import { useDebounce } from './use-debounce';

test('debounces value changes', async () => {
  const { result, rerender } = renderHook(
    ({ value }) => useDebounce(value, 300),
    { initialProps: { value: 'hello' } }
  );

  rerender({ value: 'world' });
  expect(result.current).toBe('hello'); // Not yet updated

  await waitFor(() => {
    expect(result.current).toBe('world');
  });
});

// API hook test with MSW
import { server } from '@/mocks/server';
import { http, HttpResponse } from 'msw';

test('fetches projects', async () => {
  server.use(
    http.get('/api/projects', () =>
      HttpResponse.json([{ id: '1', name: 'My Project' }])
    )
  );

  const { result } = renderHook(() => useProjects({}), {
    wrapper: QueryClientProvider,
  });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toHaveLength(1);
});

// Accessibility test with jest-axe
import { axe, toHaveNoViolations } from 'jest-axe';
expect.extend(toHaveNoViolations);

test('ProfileForm has no accessibility violations', async () => {
  const { container } = render(<ProfileForm defaultValues={{ name: 'Alice', email: 'a@b.com' }} />);
  const results = await axe(container);
  expect(results).toHaveNoViolations();
});
```

### E2E Testing (Playwright)

```ts
// tests/e2e/profile.spec.ts
import { test, expect } from '@playwright/test';

test('user can update their profile', async ({ page }) => {
  await page.goto('/profile');
  await page.getByLabel('Name').fill('Alice');
  await page.getByRole('button', { name: /save/i }).click();
  await expect(page.getByText('Profile saved')).toBeVisible();
});
```

**Test toolchain:**
- `@testing-library/react` + `@testing-library/user-event` — unit/component tests (behavior-driven)
- `jest-axe` — accessibility assertion (`expect(results).toHaveNoViolations()`) — run on all interactive components
- `msw` (Mock Service Worker) — API mocking in unit tests (intercepts at network level, no adapter mocks)
- `@playwright/test` — E2E tests in `tests/e2e/` against a running dev or staging server
- Coverage: `jest --coverage` with Istanbul; threshold in `tests/coverage/thresholds.json`

## Common Mistakes

| Mistake | Impact | Fix |
|---------|--------|-----|
| Prop drilling through 5+ levels | Brittle, hard to refactor | Use composition (children), context for truly global state, or Zustand |
| `useEffect` for derived state | Extra re-render, stale values | Compute during render: `const fullName = first + ' ' + last;` |
| Missing cleanup in `useEffect` | Memory leaks, stale subscriptions | Always return cleanup function for subscriptions, timers, AbortControllers |
| Using `useEffect` to sync state with props | Unnecessary complexity, race conditions | Use the prop directly or compute from it — `useEffect` is almost never the answer for prop-to-state sync |
| `useState` + `useEffect` for API data | No caching, no deduplication, manual loading/error handling | Use React Query |
| Defining components inside other components | Re-created every render, destroys children state | Define all components at module scope |
| Not memoizing expensive computations | Slow renders on every state change | Use `useMemo` for expensive calculations, but do not over-memoize |
| Index as key in dynamic lists | Wrong elements update on reorder/delete | Use stable unique IDs from data |
| Fetching in `useEffect` without AbortController | Race conditions on fast navigation | Use React Query (handles cancellation) or pass AbortController signal |
| Giant context providers | Every consumer re-renders on any change | Split contexts by update frequency, or use Zustand instead |
