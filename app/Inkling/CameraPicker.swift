import SwiftUI
import UIKit

/// System camera, full screen. On capture the photo is downscaled and
/// JPEG-encoded here so callers only ever see upload-ready bytes.
struct CameraPicker: UIViewControllerRepresentable {
    /// Longest side after downscale; keeps uploads ~well under the server's
    /// 5MB photo cap while staying plenty for the model to read text in shot.
    static let maxDimension: CGFloat = 2_048

    let onPhoto: (Data) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ picker: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onPhoto: onPhoto) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onPhoto: (Data) -> Void

        init(onPhoto: @escaping (Data) -> Void) {
            self.onPhoto = onPhoto
        }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            picker.dismiss(animated: true)
            guard let image = info[.originalImage] as? UIImage else { return }
            let onPhoto = onPhoto
            // Downscale + encode off the main thread; a 12MP frame is heavy.
            DispatchQueue.global(qos: .userInitiated).async {
                guard let data = Self.jpegData(from: image) else { return }
                DispatchQueue.main.async { onPhoto(data) }
            }
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            picker.dismiss(animated: true)
        }

        static func jpegData(from image: UIImage) -> Data? {
            let longest = max(image.size.width, image.size.height)
            let scale = min(1, CameraPicker.maxDimension / max(longest, 1))
            let target = CGSize(
                width: image.size.width * scale, height: image.size.height * scale
            )
            let format = UIGraphicsImageRendererFormat()
            format.scale = 1
            let scaled = UIGraphicsImageRenderer(size: target, format: format).image { _ in
                image.draw(in: CGRect(origin: .zero, size: target))
            }
            return scaled.jpegData(compressionQuality: 0.7)
        }
    }
}
