// PNG output for --snapshot renders (Snapshot.swift, GroupEditor.swift, Onboarding*.swift).
//
// A bitmap made by bitmapImageRepForCachingDisplay is in the display's colour space, and writing it as PNG embeds that
// display's ICC profile (an iCCP chunk). The profile carries the display's make, model and serial number, and the renders
// become public test goldens. So the picture is converted to generic sRGB first, encoded with ImageIO without any
// properties, and every chunk that can carry device or personal data is dropped from the bytes: iCCP (profile), eXIf,
// tEXt/zTXt/iTXt (text) and tIME. What stays is the pixels and the structural chunks. Profile bytes are never printed.

import AppKit
import ImageIO
import UniformTypeIdentifiers

/// PNG chunks that are removed from every snapshot.
let snapshotPNGDroppedChunks: Set<String> = ["iCCP", "eXIf", "tEXt", "zTXt", "iTXt", "tIME"]

/// The PNG bytes for a snapshot bitmap: generic sRGB pixels, no embedded profile, no metadata chunks. nil if encoding fails.
func snapshotPNGData(_ rep: NSBitmapImageRep) -> Data? {
    let converted = rep.converting(to: .sRGB, renderingIntent: .default) ?? rep
    guard let image = converted.cgImage else { return nil }
    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { return nil }
    return strippingPNGChunks(out as Data, dropping: snapshotPNGDroppedChunks)
}

/// The same PNG without the named chunks (their four-letter types). Pure: a test can call it with bytes it built.
/// Data that is not a PNG is returned as it is.
func strippingPNGChunks(_ png: Data, dropping dropped: Set<String>) -> Data {
    let signature: [UInt8] = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]
    let bytes = [UInt8](png)
    guard bytes.count > 8, Array(bytes[0..<8]) == signature else { return png }
    var out = Data(bytes[0..<8])
    var i = 8
    while i + 12 <= bytes.count {
        let length = Int(bytes[i]) << 24 | Int(bytes[i + 1]) << 16 | Int(bytes[i + 2]) << 8 | Int(bytes[i + 3])
        let type = String(decoding: bytes[(i + 4)..<(i + 8)], as: UTF8.self)
        let end = i + 12 + length                       // length, type, data, CRC
        guard end <= bytes.count else { return png }    // damaged: leave it alone
        if !dropped.contains(type) { out.append(contentsOf: bytes[i..<end]) }
        i = end
    }
    return out
}

/// The chunk types of a PNG, in order (a check for tests and for reading a render's structure).
func pngChunkTypes(_ png: Data) -> [String] {
    let bytes = [UInt8](png)
    var types: [String] = []
    var i = 8
    while i + 12 <= bytes.count {
        let length = Int(bytes[i]) << 24 | Int(bytes[i + 1]) << 16 | Int(bytes[i + 2]) << 8 | Int(bytes[i + 3])
        types.append(String(decoding: bytes[(i + 4)..<(i + 8)], as: UTF8.self))
        i += 12 + length
    }
    return types
}
