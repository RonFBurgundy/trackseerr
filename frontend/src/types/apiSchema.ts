import type { components } from './api';

/** A backend schema by name, generated from the OpenAPI contract: `Schema<'ManualImportItem'>`. */
export type Schema<K extends keyof components['schemas']> = components['schemas'][K];
