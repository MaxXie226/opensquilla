import { createHash } from 'node:crypto'
import {
  copyFile,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import {
  apiReportPath,
  generateApiReport,
  manifestPath,
  readJson,
  repositoryRoot,
  stableJson,
  validatePackageManifest,
} from './ui_package_api.mjs'

const compatibilityMatrixPath = path.join(
  repositoryRoot,
  'packages',
  'ui-compatibility-matrix.json',
)
const npmEntry = process.env.npm_execpath

function run(command, args, cwd = repositoryRoot) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: 'utf8',
    env: {
      ...process.env,
      npm_config_audit: 'false',
      npm_config_fund: 'false',
    },
  })
  if (result.status !== 0) {
    throw new Error(
      [`${path.basename(command)} ${args.join(' ')} failed`, result.stdout, result.stderr]
        .filter(Boolean)
        .join('\n'),
    )
  }
  return result.stdout.trim()
}

function git(...args) {
  return run('git', ['-C', repositoryRoot, ...args])
}

async function sha256(source) {
  const hash = createHash('sha256')
  hash.update(await readFile(source))
  return hash.digest('hex')
}

function safeRef(value, label) {
  if (
    typeof value !== 'string'
    || !value
    || value.length > 160
    || [...value].some((character) => character.charCodeAt(0) < 32)
  ) {
    throw new Error(`${label} must be a non-empty safe string`)
  }
  return value
}

function safeCommit(value) {
  const commit = value.trim().toLowerCase()
  if (!/^[0-9a-f]{7,64}$/.test(commit)) {
    throw new Error('source commit must contain 7-64 lowercase hexadecimal characters')
  }
  return commit
}

function packageId(name) {
  return `SPDXRef-Package-${name.replace(/[^A-Za-z0-9.-]+/g, '-')}`
}

function versionTuple(value, label) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$/.exec(value)
  if (!match) throw new Error(`${label} must use a valid semantic version`)
  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

function compareTuple(left, right) {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] < right[index] ? -1 : 1
  }
  return 0
}

function satisfiesBoundedRange(version, range) {
  const match = /^>=(\d+\.\d+\.\d+) <(\d+\.\d+\.\d+)$/.exec(range)
  if (!match) {
    throw new Error('clientSdkRange must use the form >=<semver> <<semver>')
  }
  const candidate = versionTuple(version, 'Client SDK version')
  return (
    compareTuple(candidate, versionTuple(match[1], 'Client SDK lower bound')) >= 0
    && compareTuple(candidate, versionTuple(match[2], 'Client SDK upper bound')) < 0
  )
}

async function assertEmptyOutput(outputDir) {
  try {
    const info = await stat(outputDir)
    if (!info.isDirectory()) throw new Error(`${outputDir} is not a directory`)
    const entries = await readdir(outputDir)
    if (entries.length > 0) {
      throw new Error(
        `release output directory is not empty; immutable assets will not be overwritten: ${outputDir}`,
      )
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
    await mkdir(outputDir, { recursive: true })
  }
}

function validateCompatibilityMatrix(matrix, manifest, contract) {
  if (matrix.schemaVersion !== 1) throw new Error('Unsupported compatibility matrix schema')
  if (matrix.policy !== manifest.compatibilityPolicy) {
    throw new Error('Compatibility matrix policy differs from the package manifest')
  }
  if (!matrix.current || typeof matrix.current !== 'object') {
    throw new Error('Compatibility matrix current release is missing')
  }
  const expectedVersions = Object.fromEntries(
    manifest.packages.map((record) => [record.name, record.version]),
  )
  if (
    Object.keys(matrix.current.packages).length !== Object.keys(expectedVersions).length
    || Object.entries(expectedVersions).some(
      ([name, version]) => matrix.current.packages[name] !== version,
    )
  ) {
    throw new Error('Compatibility matrix package versions are stale')
  }
  const foundationGroup = manifest.releaseGroups.find((group) => group.id === 'ui-foundation')
  const foundationVersion = manifest.packages.find(
    (record) => record.name === foundationGroup.packages[0],
  ).version
  if (matrix.current.releaseVersion !== foundationVersion) {
    throw new Error('Compatibility matrix release version differs from the Foundation group')
  }
  if (matrix.current.gateway.contractDigest !== contract.digest) {
    throw new Error('Compatibility matrix Gateway contract digest is stale')
  }
  if (
    !Number.isInteger(matrix.current.gateway.protocolMin)
    || !Number.isInteger(matrix.current.gateway.protocolMax)
    || matrix.current.gateway.protocolMin > matrix.current.gateway.protocolMax
  ) {
    throw new Error('Compatibility matrix Gateway protocol range is invalid')
  }
  if (matrix.previous === null && matrix.bootstrap !== true) {
    throw new Error('Only an explicit bootstrap release may omit the N-1 matrix entry')
  }
  if (matrix.previous !== null && matrix.bootstrap === true) {
    throw new Error('A release with an N-1 entry may not remain in bootstrap mode')
  }
  return foundationVersion
}

async function packPackage(record, outputDir, temporaryRoot) {
  if (!npmEntry) throw new Error('Run release packaging through npm so npm_execpath is available')
  const packageRoot = path.join(repositoryRoot, record.path)
  const firstRoot = path.join(temporaryRoot, 'pack-a')
  const secondRoot = path.join(temporaryRoot, 'pack-b')
  await mkdir(firstRoot, { recursive: true })
  await mkdir(secondRoot, { recursive: true })
  const pack = (destination) => {
    const output = run(
      process.execPath,
      [
        npmEntry,
        'pack',
        '--json',
        '--ignore-scripts',
        '--pack-destination',
        destination,
        packageRoot,
      ],
    )
    const [result] = JSON.parse(output)
    if (!result?.filename) throw new Error(`${record.name}: npm pack returned no tarball`)
    return result
  }
  const first = pack(firstRoot)
  const second = pack(secondRoot)
  if (first.filename !== second.filename) {
    throw new Error(`${record.name}: repeated npm pack changed the tarball name`)
  }
  const firstPath = path.join(firstRoot, first.filename)
  const secondPath = path.join(secondRoot, second.filename)
  const firstDigest = await sha256(firstPath)
  const secondDigest = await sha256(secondPath)
  if (firstDigest !== secondDigest) {
    throw new Error(`${record.name}: repeated npm pack was not byte-for-byte deterministic`)
  }
  const outputPath = path.join(outputDir, first.filename)
  await copyFile(firstPath, outputPath)
  return {
    name: first.filename,
    package: record.name,
    version: record.version,
    sha256: firstDigest,
    integrity: first.integrity,
    shasum: first.shasum,
    size: (await stat(outputPath)).size,
  }
}

function buildSbom(releaseVersion, sourceRef, sourceCommit, packages, artifacts) {
  const namespaceHash = createHash('sha256')
    .update(stableJson(artifacts.map((entry) => [entry.name, entry.sha256])))
    .digest('hex')
  const relationships = []
  for (const entry of packages) {
    const packageJson = entry.packageJson
    for (const dependency of Object.keys(packageJson.dependencies ?? {})) {
      if (!packages.some((candidate) => candidate.record.name === dependency)) continue
      relationships.push({
        spdxElementId: packageId(entry.record.name),
        relationshipType: 'DEPENDS_ON',
        relatedSpdxElement: packageId(dependency),
      })
    }
  }
  return {
    spdxVersion: 'SPDX-2.3',
    dataLicense: 'CC0-1.0',
    SPDXID: 'SPDXRef-DOCUMENT',
    name: `OpenSquilla UI Foundation ${releaseVersion}`,
    documentNamespace: `https://github.com/opensquilla/opensquilla/ui-sbom/${namespaceHash}`,
    creationInfo: {
      created: '1980-01-01T00:00:00Z',
      creators: ['Tool: scripts/build_ui_package_release.mjs'],
    },
    documentDescribes: packages.map((entry) => packageId(entry.record.name)),
    packages: packages.map((entry) => ({
      SPDXID: packageId(entry.record.name),
      name: entry.record.name,
      versionInfo: entry.record.version,
      downloadLocation: 'NOASSERTION',
      filesAnalyzed: false,
      licenseConcluded: 'Apache-2.0',
      licenseDeclared: 'Apache-2.0',
      supplier: 'Organization: OpenSquilla',
      externalRefs: [
        {
          referenceCategory: 'PACKAGE-MANAGER',
          referenceType: 'purl',
          referenceLocator: `pkg:npm/${encodeURIComponent(entry.record.name)}@${entry.record.version}`,
        },
      ],
      checksums: [
        {
          algorithm: 'SHA256',
          checksumValue: artifacts.find(
            (artifact) => artifact.package === entry.record.name,
          ).sha256,
        },
      ],
    })),
    relationships: [
      ...packages.map((entry) => ({
        spdxElementId: 'SPDXRef-DOCUMENT',
        relationshipType: 'DESCRIBES',
        relatedSpdxElement: packageId(entry.record.name),
      })),
      ...relationships,
    ],
    annotations: [
      {
        annotationType: 'OTHER',
        annotator: 'Tool: scripts/build_ui_package_release.mjs',
        annotationDate: '1980-01-01T00:00:00Z',
        comment: `Source ${sourceRef} at ${sourceCommit}`,
      },
    ],
  }
}

function buildProvenance(sourceRef, sourceCommit, releaseVersion, subjects) {
  return {
    _type: 'https://in-toto.io/Statement/v1',
    subject: subjects.map((entry) => ({
      name: entry.name,
      digest: { sha256: entry.sha256 },
    })),
    predicateType: 'https://slsa.dev/provenance/v1',
    predicate: {
      buildDefinition: {
        buildType: 'https://github.com/opensquilla/opensquilla/ui-foundation-release/v1',
        externalParameters: {
          releaseTrain: 'public-ui-foundation',
          releaseVersion,
          sourceRef,
        },
        internalParameters: {},
        resolvedDependencies: [
          {
            uri: 'git+https://github.com/opensquilla/opensquilla',
            digest: { gitCommit: sourceCommit },
          },
        ],
      },
      runDetails: {
        builder: {
          id: 'https://github.com/opensquilla/opensquilla/.github/workflows/ui-foundation-release.yml',
        },
        metadata: {
          invocationId: `${sourceRef}@${sourceCommit}`,
        },
      },
    },
  }
}

function parseArgs(argv) {
  const options = {
    outputDir: null,
    releaseVersion: null,
    sourceCommit: process.env.GITHUB_SHA || git('rev-parse', 'HEAD'),
    sourceRef:
      process.env.GITHUB_REF_NAME
      || git('symbolic-ref', '--quiet', '--short', 'HEAD')
      || 'detached',
  }
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index]
    if (value === '--output-dir') options.outputDir = path.resolve(argv[++index])
    else if (value === '--release-version') options.releaseVersion = argv[++index]
    else if (value === '--source-commit') options.sourceCommit = argv[++index]
    else if (value === '--source-ref') options.sourceRef = argv[++index]
    else throw new Error(`Unknown argument: ${value}`)
  }
  if (!options.outputDir) throw new Error('--output-dir is required')
  options.sourceRef = safeRef(options.sourceRef, 'source ref')
  options.sourceCommit = safeCommit(options.sourceCommit)
  return options
}

export async function buildRelease(options) {
  await assertEmptyOutput(options.outputDir)
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'opensquilla-ui-release-'))
  try {
    const manifest = validatePackageManifest(await readJson(manifestPath))
    const matrix = await readJson(compatibilityMatrixPath)
    const contract = await readJson(
      path.join(repositoryRoot, 'contracts', 'client', 'v3', 'contract.json'),
    )
    const releaseVersion = validateCompatibilityMatrix(matrix, manifest, contract)
    if (options.releaseVersion && options.releaseVersion !== releaseVersion) {
      throw new Error(
        `requested release ${options.releaseVersion} differs from manifest ${releaseVersion}`,
      )
    }
    const checkedInApi = await readJson(apiReportPath)
    const generatedApi = await generateApiReport()
    if (stableJson(checkedInApi) !== stableJson(generatedApi)) {
      throw new Error('Checked-in UI package API report is stale')
    }

    const packageMetadata = []
    const tarballs = []
    for (const record of manifest.packages) {
      const packageJson = await readJson(path.join(repositoryRoot, record.path, 'package.json'))
      if (packageJson.private === true) throw new Error(`${record.name} must be publishable`)
      if (
        packageJson.publishConfig?.access !== 'public'
        || packageJson.publishConfig?.provenance !== true
      ) {
        throw new Error(`${record.name} must declare public provenance-enabled publishing`)
      }
      packageMetadata.push({ record, packageJson })
      tarballs.push(await packPackage(record, options.outputDir, temporaryRoot))
    }
    const foundationPackage = packageMetadata.find(
      (entry) => entry.record.name === '@opensquilla/ui-foundation',
    ).packageJson
    if (
      foundationPackage.dependencies?.['@opensquilla/client-sdk']
      !== matrix.current.clientSdkRange
    ) {
      throw new Error(
        'UI Foundation package dependency differs from the declared Client SDK range',
      )
    }
    const currentSdkVersion = matrix.current.packages['@opensquilla/client-sdk']
    if (!satisfiesBoundedRange(currentSdkVersion, matrix.current.clientSdkRange)) {
      throw new Error('Current Client SDK is outside the UI Foundation support range')
    }
    if (
      matrix.previous
      && !satisfiesBoundedRange(
        matrix.previous.packages['@opensquilla/client-sdk'],
        matrix.current.clientSdkRange,
      )
    ) {
      throw new Error('N-1 Client SDK is outside the UI Foundation support range')
    }

    const prefix = `opensquilla-ui-foundation-${releaseVersion}`
    const apiName = `${prefix}.api-report.json`
    await copyFile(apiReportPath, path.join(options.outputDir, apiName))
    const apiArtifact = {
      name: apiName,
      sha256: await sha256(path.join(options.outputDir, apiName)),
      size: (await stat(path.join(options.outputDir, apiName))).size,
    }

    const changelogName = `${prefix}.CHANGELOG.md`
    const changelog = []
    for (const { record } of packageMetadata) {
      changelog.push(
        `## ${record.name}\n`,
        (await readFile(path.join(repositoryRoot, record.path, 'CHANGELOG.md'), 'utf8')).trim(),
        '',
      )
    }
    await writeFile(path.join(options.outputDir, changelogName), `${changelog.join('\n')}\n`)
    const changelogArtifact = {
      name: changelogName,
      sha256: await sha256(path.join(options.outputDir, changelogName)),
      size: (await stat(path.join(options.outputDir, changelogName))).size,
    }

    const compatibilityName = `${prefix}.compatibility.json`
    const compatibility = {
      schemaVersion: 1,
      policy: matrix.policy,
      status: matrix.bootstrap ? 'bootstrap-compatible' : 'compatible',
      current: matrix.current,
      previous: matrix.previous,
      requiredCombinations: matrix.bootstrap
        ? [
            {
              uiFoundation: matrix.current.releaseVersion,
              clientSdk: matrix.current.packages['@opensquilla/client-sdk'],
              gatewayProtocol: `${matrix.current.gateway.protocolMin}-${matrix.current.gateway.protocolMax}`,
            },
          ]
        : [
            {
              uiFoundation: matrix.current.releaseVersion,
              clientSdk: matrix.current.packages['@opensquilla/client-sdk'],
              gatewayProtocol: `${matrix.current.gateway.protocolMin}-${matrix.current.gateway.protocolMax}`,
            },
            {
              uiFoundation: matrix.current.releaseVersion,
              clientSdk: matrix.previous.packages['@opensquilla/client-sdk'],
              gatewayProtocol: `${matrix.previous.gateway.protocolMin}-${matrix.previous.gateway.protocolMax}`,
            },
            {
              uiFoundation: matrix.previous.releaseVersion,
              clientSdk: matrix.current.packages['@opensquilla/client-sdk'],
              gatewayProtocol: `${matrix.current.gateway.protocolMin}-${matrix.current.gateway.protocolMax}`,
            },
          ],
      evidence: [
        'external-pack-install',
        'typescript-public-api',
        'runtime-public-exports',
        'gateway-current-legacy-and-incompatible',
        'public-webui-tarball-build',
      ],
      failurePolicy: matrix.failurePolicy,
    }
    await writeFile(
      path.join(options.outputDir, compatibilityName),
      stableJson(compatibility),
    )
    const compatibilityArtifact = {
      name: compatibilityName,
      sha256: await sha256(path.join(options.outputDir, compatibilityName)),
      size: (await stat(path.join(options.outputDir, compatibilityName))).size,
    }

    const sbomName = `${prefix}.sbom.spdx.json`
    const sbom = buildSbom(
      releaseVersion,
      options.sourceRef,
      options.sourceCommit,
      packageMetadata,
      tarballs,
    )
    await writeFile(path.join(options.outputDir, sbomName), stableJson(sbom))
    const sbomArtifact = {
      name: sbomName,
      sha256: await sha256(path.join(options.outputDir, sbomName)),
      size: (await stat(path.join(options.outputDir, sbomName))).size,
    }

    const provenanceSubjects = [
      ...tarballs,
      apiArtifact,
      changelogArtifact,
      compatibilityArtifact,
      sbomArtifact,
    ]
    const provenanceName = `${prefix}.provenance.json`
    await writeFile(
      path.join(options.outputDir, provenanceName),
      stableJson(
        buildProvenance(
          options.sourceRef,
          options.sourceCommit,
          releaseVersion,
          provenanceSubjects,
        ),
      ),
    )
    const provenanceArtifact = {
      name: provenanceName,
      sha256: await sha256(path.join(options.outputDir, provenanceName)),
      size: (await stat(path.join(options.outputDir, provenanceName))).size,
    }

    const releaseManifestName = `${prefix}.manifest.json`
    const releaseManifest = {
      schemaVersion: 1,
      releaseTrain: manifest.releaseTrain,
      releaseVersion,
      immutable: true,
      source: {
        repository: 'opensquilla/opensquilla',
        ref: options.sourceRef,
        commit: options.sourceCommit,
      },
      packages: tarballs.map((entry) => ({
        name: entry.package,
        version: entry.version,
        tarball: entry.name,
        sha256: entry.sha256,
        integrity: entry.integrity,
        size: entry.size,
      })),
      compatibility: {
        policy: matrix.policy,
        report: compatibilityName,
        status: compatibility.status,
      },
      artifacts: [
        ...tarballs,
        apiArtifact,
        changelogArtifact,
        compatibilityArtifact,
        sbomArtifact,
        provenanceArtifact,
      ].map(({ name, sha256: digest, size }) => ({ name, sha256: digest, size })),
    }
    await writeFile(
      path.join(options.outputDir, releaseManifestName),
      stableJson(releaseManifest),
    )
    const manifestArtifact = {
      name: releaseManifestName,
      sha256: await sha256(path.join(options.outputDir, releaseManifestName)),
      size: (await stat(path.join(options.outputDir, releaseManifestName))).size,
    }

    const sumsName = `SHA256SUMS.ui-foundation-${releaseVersion}`
    const sums = [...releaseManifest.artifacts, manifestArtifact]
      .sort((left, right) => left.name.localeCompare(right.name))
      .map((entry) => `${entry.sha256}  ${entry.name}`)
      .join('\n')
    await writeFile(path.join(options.outputDir, sumsName), `${sums}\n`)
    console.log(
      `Built ${tarballs.length} immutable UI package tarballs for ${releaseVersion}`,
    )
    return {
      releaseVersion,
      manifest: path.join(options.outputDir, releaseManifestName),
      checksums: path.join(options.outputDir, sumsName),
      tarballs,
    }
  } catch (error) {
    await rm(options.outputDir, { recursive: true, force: true })
    throw error
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true })
  }
}

export async function main(argv = process.argv.slice(2)) {
  return buildRelease(parseArgs(argv))
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch((error) => {
    console.error(`UI package release build failed: ${error.message}`)
    process.exitCode = 1
  })
}
