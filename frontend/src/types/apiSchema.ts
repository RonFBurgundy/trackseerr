import type { components } from './api';

/** A backend schema by name, generated from the OpenAPI contract: `Schema<'ManualImportItem'>`. */
export type Schema<K extends keyof components['schemas']> = components['schemas'][K];

/**
 * A generated schema with some `string` fields narrowed to the literal unions the UI switches on.
 * Use only where the backend declares a bare `str` for a closed set of values; drop it once the backend model is a Literal.
 */
export type Narrow<T, N> = Omit<T, keyof N> & N;
