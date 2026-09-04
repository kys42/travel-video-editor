import AVFoundation
import CoreMedia
import Foundation
import Vision

struct FrameScore: Codable {
    let timestamp: Double
    let duration: Double
    let overallScore: Float
    let isUtility: Bool
    let confidence: Float
}

struct SelectedFrame: Codable {
    let timestamp: Double
    let overallScore: Float
    let isUtility: Bool
    let nearestSelectedDistance: Double?
}

struct BenchmarkResult: Codable {
    let schemaVersion: String
    let input: String
    let intervalSeconds: Double
    let mode: String
    let startedAt: String
    let elapsedSeconds: Double
    let frameCount: Int
    let featurePrintCount: Int
    let framesPerSecond: Double
    let sourceDurationSeconds: Double?
    let sourceSecondsPerWallSecond: Double?
    let scores: [FrameScore]
    let selected: [SelectedFrame]
}

enum BenchmarkError: Error, CustomStringConvertible {
    case usage(String)
    case unsupportedOS
    case missingTimestamp(String)

    var description: String {
        switch self {
        case .usage(let message): return message
        case .unsupportedOS: return "Apple Vision aesthetics scoring requires macOS 15 or newer"
        case .missingTimestamp(let request): return "Vision returned a \(request) observation without a video timestamp"
        }
    }
}

struct Arguments {
    let input: URL
    let interval: Double
    let output: URL
    let includeFeaturePrints: Bool
    let topCount: Int
    let minimumTemporalSpacing: Double
    let maximumFeatureDistance: Double

    static func parse(_ raw: [String]) throws -> Arguments {
        var values: [String: String] = [:]
        var flags = Set<String>()
        var index = 0
        while index < raw.count {
            let key = raw[index]
            if key == "--feature-prints" {
                flags.insert(key)
                index += 1
                continue
            }
            guard key.hasPrefix("--"), index + 1 < raw.count else {
                throw BenchmarkError.usage(usage)
            }
            values[key] = raw[index + 1]
            index += 2
        }

        guard
            let inputPath = values["--input"],
            let intervalText = values["--interval"],
            let interval = Double(intervalText), interval > 0,
            let outputPath = values["--output"]
        else {
            throw BenchmarkError.usage(usage)
        }

        let topCount = Int(values["--top"] ?? "12") ?? 12
        let minimumTemporalSpacing = Double(values["--min-spacing"] ?? "2.0") ?? 2.0
        let maximumFeatureDistance = Double(values["--feature-distance"] ?? "0.35") ?? 0.35
        guard topCount > 0, minimumTemporalSpacing >= 0, maximumFeatureDistance >= 0 else {
            throw BenchmarkError.usage(usage)
        }

        return Arguments(
            input: URL(fileURLWithPath: inputPath).standardizedFileURL,
            interval: interval,
            output: URL(fileURLWithPath: outputPath).standardizedFileURL,
            includeFeaturePrints: flags.contains("--feature-prints"),
            topCount: topCount,
            minimumTemporalSpacing: minimumTemporalSpacing,
            maximumFeatureDistance: maximumFeatureDistance
        )
    }

    static let usage = """
    Usage: apple-vision-frame-benchmark \\
      --input /path/to/video.mp4 --interval 1.0 --output result.json \\
      [--feature-prints] [--top 12] [--min-spacing 2.0] [--feature-distance 0.35]
    """
}

@available(macOS 15.0, *)
func timestamp(_ range: CMTimeRange?, request: String) throws -> (seconds: Double, duration: Double) {
    guard let range else { throw BenchmarkError.missingTimestamp(request) }
    let seconds = CMTimeGetSeconds(range.start)
    let duration = CMTimeGetSeconds(range.duration)
    guard seconds.isFinite else { throw BenchmarkError.missingTimestamp(request) }
    return (seconds, duration.isFinite ? duration : 0)
}

@available(macOS 15.0, *)
func selectFrames(
    scores: [FrameScore],
    features: [(timestamp: Double, observation: FeaturePrintObservation)],
    count: Int,
    minimumTemporalSpacing: Double,
    maximumFeatureDistance: Double
) throws -> [SelectedFrame] {
    let ranked = scores.sorted {
        if $0.isUtility != $1.isUtility { return !$0.isUtility }
        return $0.overallScore > $1.overallScore
    }
    var selected: [(score: FrameScore, feature: FeaturePrintObservation?)] = []

    for score in ranked {
        guard selected.count < count else { break }
        if selected.contains(where: { abs($0.score.timestamp - score.timestamp) < minimumTemporalSpacing }) {
            continue
        }

        let feature = features.min(by: {
            abs($0.timestamp - score.timestamp) < abs($1.timestamp - score.timestamp)
        })?.observation
        var nearestDistance: Double?
        if let feature {
            for existing in selected.compactMap(\.feature) {
                let distance = try feature.distance(to: existing)
                nearestDistance = min(nearestDistance ?? distance, distance)
            }
            if let nearestDistance, nearestDistance < maximumFeatureDistance {
                continue
            }
        }

        selected.append((score, feature))
    }

    return selected.map {
        let feature = $0.feature
        var nearestDistance: Double?
        if let feature {
            for other in selected.compactMap(\.feature) where other != feature {
                if let distance = try? feature.distance(to: other) {
                    nearestDistance = min(nearestDistance ?? distance, distance)
                }
            }
        }
        return SelectedFrame(
            timestamp: $0.score.timestamp,
            overallScore: $0.score.overallScore,
            isUtility: $0.score.isUtility,
            nearestSelectedDistance: nearestDistance
        )
    }
}

@main
struct AppleVisionFrameBenchmark {
    static func main() async {
        do {
            let arguments = try Arguments.parse(Array(CommandLine.arguments.dropFirst()))
            guard #available(macOS 15.0, *) else { throw BenchmarkError.unsupportedOS }
            let result = try await run(arguments)
            try FileManager.default.createDirectory(
                at: arguments.output.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            try encoder.encode(result).write(to: arguments.output, options: .atomic)
            print("frames=\(result.frameCount) features=\(result.featurePrintCount) elapsed=\(String(format: "%.3f", result.elapsedSeconds))s fps=\(String(format: "%.2f", result.framesPerSecond))")
        } catch {
            FileHandle.standardError.write(Data("error: \(error)\n".utf8))
            Foundation.exit(2)
        }
    }

    @available(macOS 15.0, *)
    static func run(_ arguments: Arguments) async throws -> BenchmarkResult {
        let assetDuration = try await AVURLAsset(url: arguments.input).load(.duration)
        let sourceDuration = CMTimeGetSeconds(assetDuration)
        let sourceDurationSeconds = sourceDuration.isFinite ? sourceDuration : nil
        let startedAt = ISO8601DateFormatter().string(from: Date())
        let start = ContinuousClock.now
        let processor = VideoProcessor(arguments.input)
        let cadence = VideoProcessor.Cadence.timeInterval(
            CMTime(seconds: arguments.interval, preferredTimescale: 600)
        )
        let aestheticsSequence = try await processor.addRequest(
            CalculateImageAestheticsScoresRequest(),
            cadence: cadence
        )

        let aestheticsTask = Task {
            var frames: [FrameScore] = []
            for try await observation in aestheticsSequence {
                let timing = try timestamp(observation.timeRange, request: "aesthetics")
                frames.append(FrameScore(
                    timestamp: timing.seconds,
                    duration: timing.duration,
                    overallScore: observation.overallScore,
                    isUtility: observation.isUtility,
                    confidence: observation.confidence
                ))
            }
            return frames
        }

        var featuresTask: Task<[(timestamp: Double, observation: FeaturePrintObservation)], Error>?
        if arguments.includeFeaturePrints {
            let featureSequence = try await processor.addRequest(
                GenerateImageFeaturePrintRequest(),
                cadence: cadence
            )
            featuresTask = Task {
                var frames: [(timestamp: Double, observation: FeaturePrintObservation)] = []
                for try await observation in featureSequence {
                    let timing = try timestamp(observation.timeRange, request: "feature print")
                    frames.append((timing.seconds, observation))
                }
                return frames
            }
        }

        processor.startAnalysis()
        let scores = try await aestheticsTask.value
        let features = try await featuresTask?.value ?? []
        let elapsed = start.duration(to: .now).components
        let elapsedSeconds = Double(elapsed.seconds) + Double(elapsed.attoseconds) / 1e18
        let selected = try selectFrames(
            scores: scores,
            features: features,
            count: arguments.topCount,
            minimumTemporalSpacing: arguments.minimumTemporalSpacing,
            maximumFeatureDistance: arguments.maximumFeatureDistance
        )
        return BenchmarkResult(
            schemaVersion: "apple-vision-frame-benchmark/v1",
            input: arguments.input.path,
            intervalSeconds: arguments.interval,
            mode: arguments.includeFeaturePrints ? "aesthetics+feature-print" : "aesthetics",
            startedAt: startedAt,
            elapsedSeconds: elapsedSeconds,
            frameCount: scores.count,
            featurePrintCount: features.count,
            framesPerSecond: scores.isEmpty ? 0 : Double(scores.count) / elapsedSeconds,
            sourceDurationSeconds: sourceDurationSeconds,
            sourceSecondsPerWallSecond: sourceDurationSeconds.map { $0 / elapsedSeconds },
            scores: scores,
            selected: selected
        )
    }
}
