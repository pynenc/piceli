// Hang guards for a loaded machine (the Python suite running beside vitest):
// a lazily loaded route can take longer than Testing Library's default 1 s
// to appear. Only the time a test may wait changes; what it waits for, and
// every assertion, stays the same.
import { configure } from '@testing-library/react';

configure({ asyncUtilTimeout: 10000 });
