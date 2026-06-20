# tests/vendor/

Vendored JS so the chat-scenario **HTML report renders fully offline** (no CDN).

## `mdx-bundle.js`

A single self-contained IIFE that bundles the MDX render toolchain and exposes
`window.renderMDX(mdxSource, element)`:

- [`@mdx-js/mdx`](https://mdxjs.com/packages/mdx/) `evaluate` (3.x) — compiles MDX in the browser
- `remark-gfm` — GFM tables
- `remark-frontmatter` — ignores the leading YAML block
- `preact` — tiny renderer; a minimal automatic-JSX runtime is built from `preact`'s
  `h`/`Fragment` (no React needed)

`tests/test_chat_scenarios.py` inlines this file into every `report_*.html`, so the
report is one self-contained, offline page.

### Rebuild (only to bump versions)

Needs Node + internet once:

```bash
mkdir /tmp/mdxbuild && cd /tmp/mdxbuild
npm init -y
npm install @mdx-js/mdx@3 preact@10 remark-gfm@4 remark-frontmatter@5 esbuild
cat > entry.js <<'EOF'
import { evaluate } from '@mdx-js/mdx';
import { h, render, Fragment } from 'preact';
import remarkGfm from 'remark-gfm';
import remarkFrontmatter from 'remark-frontmatter';
const jsx = (type, props) => h(type, (props || {}), (props || {}).children);
const runtime = { jsx, jsxs: jsx, Fragment };
export async function renderMDX(src, el) {
  const { default: Content } = await evaluate(src, {
    ...runtime, remarkPlugins: [remarkFrontmatter, remarkGfm],
    baseUrl: (typeof location !== 'undefined' ? location.href : 'file:///'),
  });
  render(h(Content), el);
}
if (typeof window !== 'undefined') window.renderMDX = renderMDX;
EOF
./node_modules/.bin/esbuild entry.js --bundle --format=iife --global-name=MDXReport \
  --minify --target=es2020 --outfile=mdx-bundle.js
cp mdx-bundle.js <repo>/tests/vendor/mdx-bundle.js
```

Pinned at build time: `@mdx-js/mdx` 3.1.1.
