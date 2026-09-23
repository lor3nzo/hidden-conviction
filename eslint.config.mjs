export default [
  {
    files: ['public/**/*.js', 'src/public_worker.js', 'tests_js/**/*.mjs'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: {
        window: 'readonly', document: 'readonly', navigator: 'readonly', fetch: 'readonly', URL: 'readonly',
        URLSearchParams: 'readonly', console: 'readonly', Intl: 'readonly', Event: 'readonly',
        HTMLElement: 'readonly', Node: 'readonly', setTimeout: 'readonly', clearTimeout: 'readonly',
        crypto: 'readonly', performance: 'readonly', Request: 'readonly', Response: 'readonly',
        Headers: 'readonly', TextEncoder: 'readonly'
      }
    },
    rules: {
      'no-undef': 'error',
      'no-unreachable': 'error',
      'no-constant-condition': ['error', { checkLoops: false }]
    }
  }
];
