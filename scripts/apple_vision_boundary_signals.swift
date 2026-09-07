import AVFoundation
import CoreMedia
import Foundation
import Vision

struct VisualSample: Codable {
    let timestamp: Double
    let duration: Double
    let featureDistanceFromPrevious: Double?
    let aestheticsScore: Float?
    let isUtility: Bool?
    let faceCount: Int
    let personCount: Int
}

struct OCRLine: Codable {
    let text: String
    let confidence: Float
}

struct OCRSample: Codable {
    let timestamp: Double
    let duration: Double
    let lines: [OCRLine]
}

struct PersonDetection: Codable {
    let timestamp: Double
    let boundingBox: [Double]
    let confidence: Float
}

struct BoundarySignalResult: Codable {
    let schemaVersion: String
    let input: String
    let visualIntervalSeconds: Double
    let ocrIntervalSeconds: Double
    let startedAt: String
    let elapsedSeconds: Double
    let sourceDurationSeconds: Double
    let faceObservations: [PersonDetection]
    let personObservations: [PersonDetection]
    let visualSamples: [VisualSample]
    let ocrSamples: [OCRSample]
}

struct FeatureSample {
    let timestamp: Double
    let duration: Double
    let distanceFromPrevious: Double?
}

struct AestheticsSample {
    let timestamp: Double
    let score: Float
    let isUtility: Bool
}

struct CountSample {
    let timestamp: Double
    let count: Int
}

enum SignalError: Error, CustomStringConvertible {
    case usage(String)
    case unsupportedOS
    case missingTimestamp(String)

    var description: String {
        switch self {
        case .usage(let message): return message
        case .unsupportedOS: return "Apple Vision boundary signals require macOS 15 or newer"
        case .missingTimestamp(let request):
            return "Vision returned a \(request) observation without a video timestamp"
        }
    }
}

struct Arguments {
    let input: URL
    let output: URL
    let visualInterval: Double
    let ocrInterval: Double

    static func parse(_ raw: [String]) throws -> Arguments {
        var values: [String: String] = [:]
        var index = 0
        while index < raw.count {
            let key = raw[index]
            guard key.hasPrefix("--"), index + 1 < raw.count else {
                throw SignalError.usage(usage)
            }
            values[key] = raw[index + 1]
            index += 2
        }

        guard
            let inputPath = values["--input"],
            let outputPath = values["--output"],
            let visualInterval = Double(values["--visual-interval"] ?? "0.333333"),
            visualInterval > 0,
            let ocrInterval = Double(values["--ocr-interval"] ?? "1.0"),
            ocrInterval > 0
        else {
            throw SignalError.usage(usage)
        }
        return Arguments(
            input: URL(fileURLWithPath: inputPath).standardizedFileURL,
            output: URL(fileURLWithPath: outputPath).standardizedFileURL,
            visualInterval: visualInterval,
            ocrInterval: ocrInterval
        )
    }

    static let usage = """
    Usage: apple-vision-boundary-signals \\
      --input /path/to/video.mp4 --output signals.json \\
      [--visual-interval 0.333333] [--ocr-interval 1.0]
    """
}

@available(macOS 15.0, *)
func timing(_ range: CMTimeRange?, request: String) throws -> (seconds: Double, duration: Double) {
    guard let range else { throw SignalError.missingTimestamp(request) }
    let seconds = CMTimeGetSeconds(range.start)
    let duration = CMTimeGetSeconds(range.duration)
    guard seconds.isFinite else { throw SignalError.missingTimestamp(request) }
    return (seconds, duration.isFinite ? duration : 0)
}

func nearest<T>(to timestamp: Double, in samples: [T], timestamp getTimestamp: (T) -> Double) -> T? {
    samples.min {
        abs(getTimestamp($0) - timestamp) < abs(getTimestamp($1) - timestamp)
    }
}

@main
struct AppleVisionBoundarySignals {
    static func main() async {
        do {
            let arguments = try Arguments.parse(Array(CommandLine.arguments.dropFirst()))
            guard #available(macOS 15.0, *) else { throw SignalError.unsupportedOS }
            let result = try await run(arguments)
            try FileManager.default.createDirectory(
                at: arguments.output.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            try encoder.encode(result).write(to: arguments.output, options: .atomic)
            print(
                "visual=\(result.visualSamples.count) ocr=\(result.ocrSamples.count) "
                    + "elapsed=\(String(format: "%.3f", result.elapsedSeconds))s"
            )
        } catch {
            FileHandle.standardError.write(Data("error: \(error)\n".utf8))
            Foundation.exit(2)
        }
    }

    @available(macOS 15.0, *)
    static func run(_ arguments: Arguments) async throws -> BoundarySignalResult {
        let assetDuration = try await AVURLAsset(url: arguments.input).load(.duration)
        let sourceDuration = CMTimeGetSeconds(assetDuration)
        guard sourceDuration.isFinite, sourceDuration > 0 else {
            throw SignalError.usage("Input video has no finite positive duration")
        }

        let processor = VideoProcessor(arguments.input)
        let visualCadence = VideoProcessor.Cadence.timeInterval(
            CMTime(seconds: arguments.visualInterval, preferredTimescale: 6000)
        )
        let ocrCadence = VideoProcessor.Cadence.timeInterval(
            CMTime(seconds: arguments.ocrInterval, preferredTimescale: 6000)
        )

        let featureSequence = try await processor.addRequest(
            GenerateImageFeaturePrintRequest(), cadence: visualCadence
        )
        let aestheticsSequence = try await processor.addRequest(
            CalculateImageAestheticsScoresRequest(), cadence: visualCadence
        )
        let faceSequence = try await processor.addRequest(
            DetectFaceRectanglesRequest(), cadence: visualCadence
        )
        var humanRequest = DetectHumanRectanglesRequest()
        humanRequest.upperBodyOnly = false
        let humanSequence = try await processor.addRequest(humanRequest, cadence: visualCadence)
        var textRequest = RecognizeTextRequest()
        textRequest.recognitionLevel = .fast
        textRequest.automaticallyDetectsLanguage = true
        textRequest.usesLanguageCorrection = false
        textRequest.minimumTextHeightFraction = 0.025
        let textSequence = try await processor.addRequest(textRequest, cadence: ocrCadence)

        let featureTask = Task {
            var result: [FeatureSample] = []
            var previous: FeaturePrintObservation?
            for try await observation in featureSequence {
                let value = try timing(observation.timeRange, request: "feature print")
                let distance = try previous.map { try observation.distance(to: $0) }
                result.append(FeatureSample(
                    timestamp: value.seconds,
                    duration: value.duration,
                    distanceFromPrevious: distance
                ))
                previous = observation
            }
            return result
        }

        let aestheticsTask = Task {
            var result: [AestheticsSample] = []
            for try await observation in aestheticsSequence {
                let value = try timing(observation.timeRange, request: "aesthetics")
                result.append(AestheticsSample(
                    timestamp: value.seconds,
                    score: observation.overallScore,
                    isUtility: observation.isUtility
                ))
            }
            return result
        }

        let faceTask = Task {
            var counts: [CountSample] = []
            var detections: [PersonDetection] = []
            var index = 0
            for try await observations in faceSequence {
                let fallback = Double(index) * arguments.visualInterval
                var sampleTime = fallback
                for observation in observations {
                    // Positive evidence must use a real video timestamp, never cadence inference.
                    let t = try timing(observation.timeRange, request: "face").seconds
                    sampleTime = t
                    let box = observation.boundingBox
                    detections.append(PersonDetection(
                        timestamp: t,
                        boundingBox: [Double(box.origin.x), Double(box.origin.y), Double(box.width), Double(box.height)],
                        confidence: observation.confidence
                    ))
                }
                counts.append(CountSample(timestamp: sampleTime, count: observations.count))
                index += 1
            }
            return (counts, detections)
        }

        let humanTask = Task {
            var counts: [CountSample] = []
            var detections: [PersonDetection] = []
            var index = 0
            for try await observations in humanSequence {
                let fallback = Double(index) * arguments.visualInterval
                var sampleTime = fallback
                for observation in observations {
                    // Positive evidence must use a real video timestamp, never cadence inference.
                    let t = try timing(observation.timeRange, request: "human").seconds
                    sampleTime = t
                    let box = observation.boundingBox
                    detections.append(PersonDetection(
                        timestamp: t,
                        boundingBox: [Double(box.origin.x), Double(box.origin.y), Double(box.width), Double(box.height)],
                        confidence: observation.confidence
                    ))
                }
                counts.append(CountSample(timestamp: sampleTime, count: observations.count))
                index += 1
            }
            return (counts, detections)
        }

        let textTask = Task {
            var result: [OCRSample] = []
            var index = 0
            for try await observations in textSequence {
                let fallback = Double(index) * arguments.ocrInterval
                let firstTiming = observations.first.flatMap { observation in
                    try? timing(observation.timeRange, request: "OCR")
                }
                let lines = observations.compactMap { observation -> OCRLine? in
                    guard let candidate = observation.topCandidates(1).first else { return nil }
                    let cleaned = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !cleaned.isEmpty else { return nil }
                    return OCRLine(text: cleaned, confidence: candidate.confidence)
                }
                result.append(OCRSample(
                    timestamp: firstTiming?.seconds ?? fallback,
                    duration: firstTiming?.duration ?? 0,
                    lines: lines
                ))
                index += 1
            }
            return result
        }

        let startedAt = ISO8601DateFormatter().string(from: Date())
        let start = ContinuousClock.now
        processor.startAnalysis()
        let features = try await featureTask.value.sorted { $0.timestamp < $1.timestamp }
        let aesthetics = try await aestheticsTask.value.sorted { $0.timestamp < $1.timestamp }
        let (faceCounts, faceDetections) = try await faceTask.value
        let faces = faceCounts.sorted { $0.timestamp < $1.timestamp }
        let (humanCounts, humanDetections) = try await humanTask.value
        let humans = humanCounts.sorted { $0.timestamp < $1.timestamp }
        let ocr = try await textTask.value.sorted { $0.timestamp < $1.timestamp }
        let elapsed = start.duration(to: .now).components
        let elapsedSeconds = Double(elapsed.seconds) + Double(elapsed.attoseconds) / 1e18

        guard !features.isEmpty else {
            throw SignalError.usage("Vision produced no visual samples; check media access and runtime permissions")
        }
        let visualSamples = features.map { feature in
            let aesthetic = nearest(to: feature.timestamp, in: aesthetics, timestamp: { $0.timestamp })
            let face = nearest(to: feature.timestamp, in: faces, timestamp: { $0.timestamp })
            let human = nearest(to: feature.timestamp, in: humans, timestamp: { $0.timestamp })
            return VisualSample(
                timestamp: feature.timestamp,
                duration: feature.duration,
                featureDistanceFromPrevious: feature.distanceFromPrevious,
                aestheticsScore: aesthetic?.score,
                isUtility: aesthetic?.isUtility,
                faceCount: face?.count ?? 0,
                personCount: human?.count ?? 0
            )
        }
        return BoundarySignalResult(
            schemaVersion: "apple-vision-boundary-signals/v1",
            input: arguments.input.path,
            visualIntervalSeconds: arguments.visualInterval,
            ocrIntervalSeconds: arguments.ocrInterval,
            startedAt: startedAt,
            elapsedSeconds: elapsedSeconds,
            sourceDurationSeconds: sourceDuration,
            faceObservations: faceDetections,
            personObservations: humanDetections,
            visualSamples: visualSamples,
            ocrSamples: ocr
        )
    }
}
