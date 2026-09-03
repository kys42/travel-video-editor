import AVFAudio
import CoreMedia
import Foundation
import Speech

struct CapabilityReport: Codable {
    let schemaVersion = "apple-speech-capabilities/v1"
    let speechTranscriberAvailable: Bool
    let supportedLocales: [String]
    let installedLocales: [String]
    let maximumReservedLocales: Int

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case speechTranscriberAvailable = "speech_transcriber_available"
        case supportedLocales = "supported_locales"
        case installedLocales = "installed_locales"
        case maximumReservedLocales = "maximum_reserved_locales"
    }
}

struct TranscriptRecord: Codable, Sendable {
    let start: Double
    let end: Double
    let text: String
    let alternatives: [String]
    let spans: [TranscriptSpan]
    let isFinal: Bool

    enum CodingKeys: String, CodingKey {
        case start, end, text, alternatives, spans
        case isFinal = "is_final"
    }
}

struct TranscriptSpan: Codable, Sendable {
    let start: Double?
    let end: Double?
    let text: String
    let confidence: Double?
}

struct DetectionRecord: Codable, Sendable {
    let start: Double
    let end: Double
    let speechDetected: Bool
    let isFinal: Bool

    enum CodingKeys: String, CodingKey {
        case start, end
        case speechDetected = "speech_detected"
        case isFinal = "is_final"
    }
}

struct TranscriptionReport: Codable {
    let schemaVersion = "apple-speech-transcription/v2"
    let input: String
    let requestedLocale: String
    let selectedLocale: String
    let detectorEnabled: Bool
    let detectorRole: String
    let detectorSensitivity: String
    let detectorResultSemantics: String
    let assetStatusBefore: String
    let assetStatusAfter: String
    let recognitionRejectedAsEmpty: Bool
    let transcripts: [TranscriptRecord]
    let detectorResults: [DetectionRecord]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case input
        case requestedLocale = "requested_locale"
        case selectedLocale = "selected_locale"
        case detectorEnabled = "detector_enabled"
        case detectorRole = "detector_role"
        case detectorSensitivity = "detector_sensitivity"
        case detectorResultSemantics = "detector_result_semantics"
        case assetStatusBefore = "asset_status_before"
        case assetStatusAfter = "asset_status_after"
        case recognitionRejectedAsEmpty = "recognition_rejected_as_empty"
        case transcripts
        case detectorResults = "detector_results"
    }
}

func isRecognitionRejected(_ error: Error) -> Bool {
    let value = error as NSError
    return value.domain == "SFSpeechErrorDomain" && value.code == 1
}

actor ResultCollector {
    private var transcripts: [TranscriptRecord] = []
    private var detections: [DetectionRecord] = []

    func addTranscript(_ record: TranscriptRecord) {
        transcripts.append(record)
    }

    func addDetection(_ record: DetectionRecord) {
        detections.append(record)
    }

    func snapshot() -> ([TranscriptRecord], [DetectionRecord]) {
        (
            transcripts.sorted { ($0.start, $0.end) < ($1.start, $1.end) },
            detections.sorted { ($0.start, $0.end) < ($1.start, $1.end) }
        )
    }
}

enum CLIError: Error, CustomStringConvertible {
    case usage(String)
    case unsupportedLocale(String)

    var description: String {
        switch self {
        case .usage(let message): message
        case .unsupportedLocale(let locale): "Unsupported Apple Speech locale: \(locale)"
        }
    }
}

func seconds(_ time: CMTime) -> Double {
    let value = CMTimeGetSeconds(time)
    return value.isFinite ? value : 0
}

func statusName(_ status: AssetInventory.Status) -> String {
    switch status {
    case .unsupported: "unsupported"
    case .supported: "supported"
    case .downloading: "downloading"
    case .installed: "installed"
    @unknown default: "unknown"
    }
}

func writeJSON<T: Encodable>(_ value: T, to output: String?) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(value)
    if let output {
        let url = URL(fileURLWithPath: output)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: url, options: .atomic)
    } else {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

func option(_ name: String, in arguments: [String]) throws -> String {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        throw CLIError.usage("Missing required option \(name)")
    }
    return arguments[index + 1]
}

func optionalOption(_ name: String, in arguments: [String]) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        return nil
    }
    return arguments[index + 1]
}

func ensureAssets(for modules: [any SpeechModule]) async throws -> (String, String) {
    let before = await AssetInventory.status(forModules: modules)
    if before != .installed,
       let request = try await AssetInventory.assetInstallationRequest(supporting: modules)
    {
        try await request.downloadAndInstall()
    }
    let after = await AssetInventory.status(forModules: modules)
    return (statusName(before), statusName(after))
}

func collectTranscripts(
    from transcriber: SpeechTranscriber,
    into collector: ResultCollector
) async throws {
    for try await result in transcriber.results {
        let start = seconds(result.range.start)
        let end = start + seconds(result.range.duration)
        let spans = result.text.runs.map { run in
            let timeRange = run.audioTimeRange
            let spanStart = timeRange.map { seconds($0.start) }
            let spanEnd = timeRange.map { seconds($0.start) + seconds($0.duration) }
            return TranscriptSpan(
                start: spanStart,
                end: spanEnd,
                text: String(result.text[run.range].characters),
                confidence: run.transcriptionConfidence
            )
        }
        await collector.addTranscript(
            TranscriptRecord(
                start: start,
                end: end,
                text: String(result.text.characters),
                alternatives: result.alternatives.map { String($0.characters) },
                spans: spans,
                isFinal: result.isFinal
            )
        )
    }
}

func detectorSensitivity(
    from arguments: [String]
) throws -> (SpeechDetector.SensitivityLevel, String) {
    let value = optionalOption("--detector-sensitivity", in: arguments) ?? "medium"
    switch value.lowercased() {
    case "low": return (.low, "low")
    case "medium": return (.medium, "medium")
    case "high": return (.high, "high")
    default:
        throw CLIError.usage(
            "Unsupported --detector-sensitivity \(value); use low, medium, or high"
        )
    }
}

func collectDetections(
    from detector: SpeechDetector,
    into collector: ResultCollector
) async throws {
    for try await result in detector.results {
        let start = seconds(result.range.start)
        let end = start + seconds(result.range.duration)
        await collector.addDetection(
            DetectionRecord(
                start: start,
                end: end,
                speechDetected: result.speechDetected,
                isFinal: result.isFinal
            )
        )
    }
}

func capabilities(output: String?) async throws {
    let report = CapabilityReport(
        speechTranscriberAvailable: SpeechTranscriber.isAvailable,
        supportedLocales: await SpeechTranscriber.supportedLocales
            .map(\.identifier)
            .sorted(),
        installedLocales: await SpeechTranscriber.installedLocales
            .map(\.identifier)
            .sorted(),
        maximumReservedLocales: AssetInventory.maximumReservedLocales
    )
    try writeJSON(report, to: output)
}

func transcribe(arguments: [String]) async throws {
    let input = try option("--input", in: arguments)
    let requestedIdentifier = try option("--locale", in: arguments)
    let output = try option("--output", in: arguments)
    let requestedLocale = Locale(identifier: requestedIdentifier)
    guard let selectedLocale = await SpeechTranscriber.supportedLocale(
        equivalentTo: requestedLocale
    ) else {
        throw CLIError.unsupportedLocale(requestedIdentifier)
    }
    let (sensitivity, sensitivityName) = try detectorSensitivity(from: arguments)

    let transcriber = SpeechTranscriber(
        locale: selectedLocale,
        transcriptionOptions: [],
        reportingOptions: [.alternativeTranscriptions],
        attributeOptions: [.audioTimeRange, .transcriptionConfidence]
    )
    let detector = SpeechDetector(
        detectionOptions: .init(sensitivityLevel: sensitivity),
        reportResults: true
    )
    let modules: [any SpeechModule] = [detector, transcriber]
    let (statusBefore, statusAfter) = try await ensureAssets(for: modules)
    let audioFile = try AVAudioFile(forReading: URL(fileURLWithPath: input))
    let collector = ResultCollector()
    let transcriptionTask = Task {
        try await collectTranscripts(from: transcriber, into: collector)
    }
    let detectionTask = Task {
        try await collectDetections(from: detector, into: collector)
    }
    let analyzer = SpeechAnalyzer(modules: modules)
    var recognitionRejected = false
    do {
        if let lastTime = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastTime)
        } else {
            await analyzer.cancelAndFinishNow()
        }
    } catch {
        guard isRecognitionRejected(error) else { throw error }
        recognitionRejected = true
        await analyzer.cancelAndFinishNow()
    }
    do {
        try await transcriptionTask.value
    } catch {
        guard isRecognitionRejected(error) else { throw error }
        recognitionRejected = true
    }
    do {
        try await detectionTask.value
    } catch {
        guard isRecognitionRejected(error) else { throw error }
        recognitionRejected = true
    }
    let (transcripts, detections) = await collector.snapshot()
    try writeJSON(
        TranscriptionReport(
            input: input,
            requestedLocale: requestedIdentifier,
            selectedLocale: selectedLocale.identifier,
            detectorEnabled: true,
            detectorRole: "vad_gating_for_transcriber",
            detectorSensitivity: sensitivityName,
            detectorResultSemantics: "error_reporting_only_on_current_macos_26_api",
            assetStatusBefore: statusBefore,
            assetStatusAfter: statusAfter,
            recognitionRejectedAsEmpty: recognitionRejected,
            transcripts: transcripts,
            detectorResults: detections
        ),
        to: output
    )
}

@main
struct AppleSpeechCLI {
    static func main() async {
        do {
            let arguments = Array(CommandLine.arguments.dropFirst())
            guard let command = arguments.first else {
                throw CLIError.usage(
                    "Usage: apple-speech capabilities [--output FILE] | "
                        + "transcribe --input AUDIO --locale LOCALE --output FILE "
                        + "[--detector-sensitivity low|medium|high]"
                )
            }
            switch command {
            case "capabilities":
                try await capabilities(output: optionalOption("--output", in: arguments))
            case "transcribe":
                try await transcribe(arguments: arguments)
            default:
                throw CLIError.usage("Unknown command: \(command)")
            }
        } catch {
            FileHandle.standardError.write(Data("error: \(error)\n".utf8))
            Foundation.exit(1)
        }
    }
}
