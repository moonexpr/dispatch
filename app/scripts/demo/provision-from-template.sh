#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# provision-from-template.sh — create + seed a demo target repo so the dispatch
# pipeline has a cloneable, deployable base before engineer:act_clone runs.
#
#   Usage: provision-from-template.sh DEST [TEMPLATE_DIR]
#     DEST          destination repo owner/name (e.g. moonexpr/dog-blog-demo)
#     TEMPLATE_DIR  convention source (default: ~/Mendotree/rbd-t3app)
#
# What it seeds: a MINIMAL, self-contained Next.js (app-router) + Tailwind dog
# blog with STUBBED interactive components (like button, comment box, newsletter
# signup — all client-state only, no backend). It follows rbd-t3app's stack
# conventions (TypeScript, app router, Tailwind, `@/` alias) but deliberately
# DROPS the template's DB-coupled weight (rbd-t3app requires DATABASE_URL and has
# DB-backed routes, so a fresh Vercel prod build of the full template would fail).
# The result builds and deploys on Vercel with no env and no database — exactly
# what a live demo needs. The engineer phase then "wires up" on top of this base.
#
# Idempotent: if DEST already exists and has commits, it is left untouched.
# The seed commit is intentionally UNSIGNED (--no-gpg-sign): this is a throwaway
# demo repo, not the framework repo, and signing would need the loopback pinentry.
# ---------------------------------------------------------------------------
set -euo pipefail

DEST="${1:?usage: provision-from-template.sh DEST [TEMPLATE_DIR]}"
TEMPLATE="${2:-$HOME/Mendotree/rbd-t3app}"
GH="${GH_BIN:-gh}"

command -v "$GH" >/dev/null 2>&1 || { echo "provision: '$GH' (GitHub CLI) not found" >&2; exit 1; }

echo "provision: target=$DEST template-conventions=$TEMPLATE"

EXISTS=0
if "$GH" repo view "$DEST" >/dev/null 2>&1; then
  if "$GH" api "repos/$DEST/commits?per_page=1" >/dev/null 2>&1; then
    echo "provision: $DEST already exists and is seeded — skipping (idempotent)"
    exit 0
  fi
  EXISTS=1
  echo "provision: $DEST exists but is empty — seeding"
else
  echo "provision: $DEST does not exist — will create (private)"
fi

# PROVISION_OUT_DIR pins the build dir (else a temp dir, auto-removed on exit).
# PROVISION_GENERATE_ONLY=1 writes the app + commits, then stops before repo
# create/push — used to smoke-build the generated app without touching GitHub.
if [ -n "${PROVISION_OUT_DIR:-}" ]; then
  WORK="$PROVISION_OUT_DIR"; mkdir -p "$WORK"
else
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
fi
w() { mkdir -p "$(dirname "$WORK/$1")"; cat > "$WORK/$1"; }

# ---- build config (minimal, stable, Vercel auto-detected) -----------------
w package.json <<'EOF'
{
  "name": "dog-blog-demo",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint"
  },
  "dependencies": {
    "next": "14.2.15",
    "react": "18.3.1",
    "react-dom": "18.3.1"
  },
  "devDependencies": {
    "@types/node": "20.14.10",
    "@types/react": "18.3.3",
    "@types/react-dom": "18.3.0",
    "autoprefixer": "10.4.19",
    "postcss": "8.4.39",
    "tailwindcss": "3.4.6",
    "typescript": "5.5.3"
  }
}
EOF

w next.config.mjs <<'EOF'
/** @type {import('next').NextConfig} */
const nextConfig = {};
export default nextConfig;
EOF

w tsconfig.json <<'EOF'
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": true,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
EOF

w postcss.config.mjs <<'EOF'
export default { plugins: { tailwindcss: {}, autoprefixer: {} } };
EOF

w tailwind.config.ts <<'EOF'
import type { Config } from "tailwindcss";

export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: { extend: {} },
  plugins: [],
} satisfies Config;
EOF

w .gitignore <<'EOF'
node_modules
.next
out
.env
.env.*
.vercel
*.tsbuildinfo
next-env.d.ts
EOF

# ---- app ------------------------------------------------------------------
w src/app/globals.css <<'EOF'
@tailwind base;
@tailwind components;
@tailwind utilities;
EOF

w src/app/layout.tsx <<'EOF'
import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "The Daily Woof — a dog blog",
  description: "A tiny demo dog blog with stubbed interactive components.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-amber-50 text-stone-800 antialiased">
        <header className="border-b border-amber-200 bg-amber-100">
          <div className="mx-auto max-w-3xl px-4 py-6">
            <h1 className="text-3xl font-bold tracking-tight">🐾 The Daily Woof</h1>
            <p className="text-sm text-stone-600">
              Good dogs, good posts. (Demo — interactions are stubbed.)
            </p>
          </div>
        </header>
        <main className="mx-auto max-w-3xl px-4 py-8">{children}</main>
        <footer className="mx-auto max-w-3xl px-4 py-10 text-xs text-stone-500">
          Built by dispatch <code>/oneshot</code> · seeded from rbd-t3app conventions
        </footer>
      </body>
    </html>
  );
}
EOF

w src/data/posts.ts <<'EOF'
export type Post = {
  slug: string;
  title: string;
  date: string;
  excerpt: string;
  body: string;
};

export const posts: Post[] = [
  {
    slug: "welcome",
    title: "Welcome to The Daily Woof",
    date: "2026-06-11",
    excerpt: "Why every good boy deserves a blog.",
    body: "This is a demo dog blog scaffolded by the dispatch oneshot pipeline. The interactive bits below are stubs — wired to local state, ready to connect to a real backend.",
  },
  {
    slug: "fetch-etiquette",
    title: "The Fine Art of Fetch",
    date: "2026-06-10",
    excerpt: "A field guide to bringing it back (mostly).",
    body: "Step one: chase the ball. Step two: consider bringing it back. Step three: negotiate. Our resident retriever weighs in.",
  },
  {
    slug: "nap-spots",
    title: "Ranking the Sunbeam Nap Spots",
    date: "2026-06-09",
    excerpt: "The living room floor is undefeated at 3pm.",
    body: "We surveyed twelve very good dogs. The verdict was unanimous, and also they fell asleep before finishing the survey.",
  },
];
EOF

w src/components/LikeButton.tsx <<'EOF'
"use client";

import { useState } from "react";

// STUB: increments local state only. Wire to a real like API/DB later.
export function LikeButton({ postSlug }: { postSlug: string }) {
  const [likes, setLikes] = useState(0);
  return (
    <button
      type="button"
      onClick={() => setLikes((n) => n + 1)}
      aria-label={`Give ${postSlug} a treat`}
      className="rounded-full bg-amber-200 px-4 py-1 text-sm font-medium transition hover:bg-amber-300"
    >
      🦴 {likes} {likes === 1 ? "treat" : "treats"}
    </button>
  );
}
EOF

w src/components/CommentBox.tsx <<'EOF'
"use client";

import { useState } from "react";

// STUB: comments live in local state only — no persistence yet.
export function CommentBox({ postSlug }: { postSlug: string }) {
  const [text, setText] = useState("");
  const [comments, setComments] = useState<string[]>([]);

  return (
    <div className="mt-5 border-t border-amber-100 pt-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!text.trim()) return;
          setComments((c) => [...c, text.trim()]);
          setText("");
        }}
        className="flex gap-2"
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={`Bark about "${postSlug}"…`}
          className="flex-1 rounded border border-amber-200 px-3 py-1 text-sm"
        />
        <button
          type="submit"
          className="rounded bg-stone-800 px-3 py-1 text-sm font-medium text-amber-50 hover:bg-stone-700"
        >
          Post
        </button>
      </form>
      <ul className="mt-3 space-y-1 text-sm text-stone-600">
        {comments.map((c, i) => (
          <li key={i} className="rounded bg-amber-100/60 px-3 py-1">🐶 {c}</li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-stone-400">Comments are stubbed (local only).</p>
    </div>
  );
}
EOF

w src/components/NewsletterSignup.tsx <<'EOF'
"use client";

import { useState } from "react";

// STUB: pretends to subscribe — no backend call is made.
export function NewsletterSignup() {
  const [email, setEmail] = useState("");
  const [done, setDone] = useState(false);

  if (done) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-100 p-4 text-sm">
        🎉 Thanks! You&apos;re on the list (well, you would be — this is stubbed).
      </div>
    );
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (email.includes("@")) setDone(true);
      }}
      className="flex flex-col gap-2 rounded-lg border border-amber-200 bg-white p-4 sm:flex-row"
    >
      <input
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="your@email.com"
        className="flex-1 rounded border border-amber-200 px-3 py-2 text-sm"
      />
      <button
        type="submit"
        className="rounded bg-amber-400 px-4 py-2 text-sm font-semibold text-stone-900 hover:bg-amber-500"
      >
        Get the weekly woof
      </button>
    </form>
  );
}
EOF

w src/app/page.tsx <<'EOF'
import { posts } from "@/data/posts";
import { LikeButton } from "@/components/LikeButton";
import { CommentBox } from "@/components/CommentBox";
import { NewsletterSignup } from "@/components/NewsletterSignup";

export default function Home() {
  return (
    <div className="space-y-8">
      <NewsletterSignup />
      {posts.map((post) => (
        <article
          key={post.slug}
          className="rounded-lg border border-amber-200 bg-white p-6 shadow-sm"
        >
          <h2 className="text-xl font-semibold">{post.title}</h2>
          <p className="text-xs text-stone-500">{post.date}</p>
          <p className="mt-3 italic text-stone-600">{post.excerpt}</p>
          <p className="mt-3 leading-relaxed">{post.body}</p>
          <div className="mt-4 flex items-center gap-4">
            <LikeButton postSlug={post.slug} />
          </div>
          <CommentBox postSlug={post.slug} />
        </article>
      ))}
    </div>
  );
}
EOF

w README.md <<'EOF'
# dog-blog-demo

A minimal Next.js (app-router) + Tailwind **dog blog** with **stubbed interactive
components** — seeded by the dispatch `/oneshot` pipeline for a live demo.

Follows the rbd-t3app stack conventions (TypeScript, app router, Tailwind, `@/`
alias) but is intentionally self-contained: no database, no required env, so it
builds and deploys on Vercel out of the box.

## Stubbed interactivity (client-state only, ready to wire to a backend)

- `LikeButton` — 🦴 treat counter, local state.
- `CommentBox` — post comments to a local list.
- `NewsletterSignup` — fake subscribe confirmation.

## Run

```bash
pnpm install   # or npm install
pnpm dev
```
EOF

# ---- commit + publish -----------------------------------------------------
cd "$WORK"
git init -q -b main
git add -A
git -c commit.gpgsign=false commit -q --no-gpg-sign \
  -m "seed: minimal dog blog demo (stubbed interactive components)"

if [ -n "${PROVISION_GENERATE_ONLY:-}" ]; then
  echo "provision: generate-only — app written + committed at $WORK (no repo push)"
  exit 0
fi

if [ "$EXISTS" = "0" ]; then
  "$GH" repo create "$DEST" --private --source=. --remote=origin --push
else
  git remote add origin "https://github.com/$DEST.git" 2>/dev/null || true
  git -c credential.helper='!'"$GH"' auth git-credential' push -u origin main
fi

echo "provision: done — https://github.com/$DEST"
