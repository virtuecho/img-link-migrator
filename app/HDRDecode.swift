import Foundation
import CoreImage
import CoreGraphics

// Render a gain-map HEIC or linear scRGB bitmap into 16-bit PQ PNG.
// The AVIF encoder subsequently quantizes these pixels to 12 bits.
do {
    let args = CommandLine.arguments
    guard args.count == 3 || args.count == 5 else {
        throw NSError(domain: "HDRDecode", code: 1, userInfo: [NSLocalizedDescriptionKey: "Expected input and output paths, with optional bitmap dimensions."])
    }
    let input = URL(fileURLWithPath: args[1])
    let output = URL(fileURLWithPath: args[2])
    let linear = CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!
    let pq = CGColorSpace(name: CGColorSpace.itur_2100_PQ)!
    let image: CIImage
    if args.count == 5 {
        guard let width = Int(args[3]), let height = Int(args[4]), width > 0, height > 0 else {
            throw NSError(domain: "HDRDecode", code: 2, userInfo: [NSLocalizedDescriptionKey: "Unavailable HDR bitmap dimensions."])
        }
        image = CIImage(bitmapData: try Data(contentsOf: input), bytesPerRow: width * 16,
                        size: CGSize(width: width, height: height), format: .RGBAf, colorSpace: linear)
    } else {
        guard #available(macOS 14.0, *) else {
            throw NSError(domain: "HDRDecode", code: 3, userInfo: [NSLocalizedDescriptionKey: "Gain-map HEIC conversion requires macOS 14 or later."])
        }
        // Use the documented key so older SDKs can still compile the macOS 13 app.
        let options: [CIImageOption: Any] = [CIImageOption(rawValue: "kCIImageExpandToHDR"): true,
                                           .applyOrientationProperty: true]
        guard let decoded = CIImage(contentsOf: input, options: options) else {
            throw NSError(domain: "HDRDecode", code: 4, userInfo: [NSLocalizedDescriptionKey: "HDR image could not be decoded."])
        }
        image = decoded
    }
    let context = CIContext(options: [.workingColorSpace: linear, .workingFormat: CIFormat.RGBAf.rawValue])
    try context.writePNGRepresentation(of: image, to: output, format: .RGBA16, colorSpace: pq, options: [:])
} catch {
    FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
    exit(1)
}
