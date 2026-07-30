import { WorkbenchPanelRegistry } from './runtime'

/** Create a composition-scoped provider registry for one product instance. */
export function createWorkbenchPanelRegistry(): WorkbenchPanelRegistry {
  return new WorkbenchPanelRegistry()
}
