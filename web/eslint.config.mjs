import { FlatCompat } from '@eslint/eslintrc';

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

export default [
  ...compat.extends('next/core-web-vitals', 'next/typescript'),
  {
    rules: {
      // SEC-WEB-02: no dangerouslySetInnerHTML anywhere in the console
      'react/no-danger': 'error',
      'no-restricted-syntax': ['error', { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
                                         message: 'SEC-WEB-02: dangerouslySetInnerHTML is banned' }],
    },
  },
];
