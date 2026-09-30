import tseslint from 'typescript-eslint';
import hooks from 'eslint-plugin-react-hooks';
export default tseslint.config(...tseslint.configs.recommended, {
  files: ['src/**/*.{ts,tsx}'],
  plugins: { 'react-hooks': hooks },
  rules: { 'react-hooks/rules-of-hooks': 'error', 'react-hooks/exhaustive-deps': 'error' },
}, { ignores: ['src/api/generated.ts'] });
