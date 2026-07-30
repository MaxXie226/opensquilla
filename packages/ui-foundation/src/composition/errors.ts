export type CompositionContractErrorCode =
  | 'invalid_feature_id'
  | 'duplicate_feature_id'
  | 'unsupported_feature_api_version'
  | 'unknown_feature_dependency'
  | 'feature_dependency_cycle'
  | 'invalid_contribution_id'
  | 'duplicate_contribution_id'
  | 'invalid_route'
  | 'duplicate_route_path'
  | 'duplicate_route_name'
  | 'unknown_page'
  | 'unknown_route'
  | 'undeclared_feature_reference'
  | 'invalid_state_namespace'
  | 'duplicate_state_namespace'
  | 'invalid_capability'
  | 'unknown_capability'
  | 'missing_required_capability'
  | 'unsupported_native_adapter_version'
  | 'state_initialization_failed'
  | 'unknown_state_namespace'
  | 'unknown_page_id'
  | 'composition_disposed'

export class CompositionContractError extends Error {
  readonly code: CompositionContractErrorCode
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: CompositionContractErrorCode,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
    options?: ErrorOptions,
  ) {
    super(message, options)
    this.name = 'CompositionContractError'
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}
